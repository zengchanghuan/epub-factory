"""R9 app-scoped runtime control fencing with real Celery handlers, offline."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class WorkerControlTests(unittest.TestCase):
    def setUp(self):
        self.network = []
        for target in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            guard = patch(target, side_effect=AssertionError('R9 control tests forbid network'))
            self.network.append(guard.start())
            self.addCleanup(guard.stop)
        from celery import Celery
        from celery.worker.control import Panel
        from app.infra.worker_control import register_worker_control_guards
        self.Panel, self.register = Panel, register_worker_control_guards
        data, meta = dict(Panel.data), dict(Panel.meta)
        self.addCleanup(lambda: (Panel.data.clear(), Panel.data.update(data),
                                 Panel.meta.clear(), Panel.meta.update(meta)))
        self.app = Celery('fixed-roles', broker='memory://', set_as_current=False)
        self.app.conf.epub_queue_isolation = True
        self.addCleanup(self.app.close)
        self.original = {name: getattr(Panel.data[name], '__wrapped__', Panel.data[name])
                         for name in ('add_consumer', 'cancel_consumer', 'pool_grow', 'pool_shrink', 'autoscale')}
        self.register()

    def tearDown(self):
        for guard in self.network:
            guard.assert_not_called()

    def test_registration_is_idempotent_and_preserves_celery_metadata(self):
        handlers, metadata = dict(self.Panel.data), dict(self.Panel.meta)
        self.register()
        self.register()
        self.assertEqual(self.Panel.data, handlers)
        self.assertEqual(self.Panel.meta, metadata)
        for name, original in self.original.items():
            self.assertIs(self.Panel.data[name].__wrapped__, original)
            self.assertEqual(self.Panel.meta[name].type, 'control')

    def test_both_roles_and_direct_queue_workers_reject_all_topology_changes(self):
        calls = [('add_consumer', {'queue': 'celery'}),
                 ('cancel_consumer', {'queue': 'housekeeping'}),
                 ('pool_grow', {'n': 2}), ('pool_shrink', {'n': 1}),
                 ('autoscale', {'max': 3, 'min': 1})]
        for role in ('book', 'housekeeping', None):
            self.app.conf.epub_worker_role = role
            for command, arguments in calls:
                with self.subTest(role=role, command=command):
                    consumer = Mock()
                    response = self.Panel.data[command](SimpleNamespace(app=self.app, consumer=consumer), **arguments)
                    self.assertIn('error', response)
                    self.assertIn(command, response['error'])
                    self.assertEqual(consumer.mock_calls, [])

    def test_other_apps_delegate_to_original_handlers_without_changing_arguments(self):
        from celery import Celery
        other = Celery('unrelated', broker='memory://', set_as_current=False)
        self.addCleanup(other.close)
        consumer = Mock()
        consumer.controller.autoscaler = None
        state = SimpleNamespace(app=other, consumer=consumer)
        self.assertEqual(self.Panel.data['add_consumer'](state, queue='extra'), {'ok': 'add consumer extra'})
        consumer.call_soon.assert_called_once_with(consumer.add_task_queue, 'extra', None, 'direct', None)
        consumer.reset_mock()
        self.assertEqual(self.Panel.data['cancel_consumer'](state, queue='extra'), {'ok': 'no longer consuming from extra'})
        consumer.call_soon.assert_called_once_with(consumer.cancel_task_queue, 'extra')
        consumer.reset_mock()
        self.assertEqual(self.Panel.data['pool_grow'](state, n=2), {'ok': 'pool will grow'})
        consumer.pool.grow.assert_called_once_with(2)
        consumer._update_prefetch_count.assert_called_once_with(2)
        consumer.reset_mock()
        self.assertEqual(self.Panel.data['pool_shrink'](state, n=1), {'ok': 'pool will shrink'})
        consumer.pool.shrink.assert_called_once_with(1)
        consumer._update_prefetch_count.assert_called_once_with(-1)
        consumer.controller.autoscaler = Mock()
        consumer.controller.autoscaler.update.return_value = (3, 1)
        self.assertEqual(self.Panel.data['autoscale'](state, max=3, min=1), {'ok': 'autoscale now max=3 min=1'})
        consumer.controller.autoscaler.update.assert_called_once_with(3, 1)

    def test_inspection_ping_and_other_management_handlers_are_unchanged(self):
        from celery.worker import control
        for name in ('ping', 'stats', 'active_queues', 'shutdown', 'revoke', 'rate_limit'):
            self.assertIs(self.Panel.data[name], getattr(control, name))
        state = SimpleNamespace(app=self.app, consumer=Mock())
        self.assertEqual(self.Panel.data['ping'](state), {'ok': 'pong'})
        state.consumer.controller.stats.return_value = {'pool': {'max-concurrency': 1}}
        self.assertEqual(self.Panel.data['stats'](state), {'pool': {'max-concurrency': 1}})

    def test_real_memory_consumer_keeps_its_queue_while_other_app_can_add_one(self):
        from celery import Celery
        from celery.worker.consumer.consumer import Consumer
        from kombu import Exchange, Queue
        for protected, queue, extra in ((True, 'celery', 'housekeeping'),
                                       (True, 'housekeeping', 'celery'),
                                       (False, 'celery', 'housekeeping')):
            with self.subTest(protected=protected, queue=queue):
                app = Celery('control-memory', broker='memory://', set_as_current=False)
                app.conf.update(epub_queue_isolation=protected,
                                task_queues=tuple(Queue(name, Exchange(name), routing_key=name)
                                                  for name in ('celery', 'housekeeping')))
                app.amqp.queues.select([queue])
                try:
                    with app.connection_for_read() as connection:
                        consumer = Consumer.__new__(Consumer)
                        consumer.app = app
                        consumer.task_consumer = app.amqp.TaskConsumer(connection.default_channel)
                        consumer.call_soon = lambda fn, *args, **kwargs: fn(*args, **kwargs)
                        state = SimpleNamespace(app=app, consumer=consumer)
                        response = self.Panel.data['add_consumer'](state, queue=extra)
                        self.assertIn('error' if protected else 'ok', response)
                        self.assertEqual([item.name for item in consumer.task_consumer.queues],
                                         [queue] if protected else [queue, extra])
                        consumer.task_consumer.cancel()
                finally:
                    app.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
