"""R9 real Celery/Kombu routing and Beat envelopes; no broker/model/network."""
import io
import os
import sys
import types
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch


class QueueRoutingTests(unittest.TestCase):
    def _patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self):
        self._patch(patch.dict(os.environ, {
            'CELERY_BROKER_URL': 'memory://', 'CELERY_RESULT_BACKEND': 'cache+memory://',
            'CELERY_WORKER_CONCURRENCY': '1', 'CELERY_TASK_SOFT_TIME_LIMIT': '1500',
            'CELERY_TASK_TIME_LIMIT': '1800', 'EPUB_BOOK_SOFT_TIME_LIMIT': '7200',
            'EPUB_BOOK_TIME_LIMIT': '7500', 'CELERY_VISIBILITY_TIMEOUT': '10800',
            'CELERY_HOUSEKEEPING_SOFT_TIME_LIMIT': '1500', 'CELERY_HOUSEKEEPING_TIME_LIMIT': '1800',
            'EPUB_PERSISTENT_STORE': '0', 'SENTRY_DSN': '',
        }))
        self._patch(patch('dotenv.load_dotenv', return_value=False))
        self.network = [self._patch(patch(name, side_effect=AssertionError('R9 forbids network')))
                        for name in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo')]
        from app.infra.celery_app import build_celery_app, validate_worker_queues, BOOK_TASKS, HOUSEKEEPING_TASKS
        from app.infra import worker
        from celery import current_app
        from kombu import Producer
        prior_app = current_app._get_current_object()
        self.addCleanup(prior_app.set_current)
        self.build = build_celery_app
        self.validate = validate_worker_queues
        self.book_tasks, self.house_tasks = BOOK_TASKS, HOUSEKEEPING_TASKS
        self.worker = worker
        self.app = self.build()
        self.addCleanup(self.app.close)
        self.app.conf.update(task_ignore_result=True, include=[])
        self.tasks = {}
        for name in (*BOOK_TASKS, *HOUSEKEEPING_TASKS):
            def operation(*args, **kwargs):
                raise AssertionError('Routing tests must not execute business tasks')
            self.tasks[name] = self.app.task(name=name, shared=False)(operation)._get_current_object()
        self.app.finalize()
        self.publish = self._patch(patch.object(Producer, 'publish', autospec=True))

    def tearDown(self):
        for network in self.network:
            network.assert_not_called()

    def assert_envelope(self, task_name, queue, *, exchange_name=''):
        self.publish.assert_called_once()
        options = self.publish.call_args.kwargs
        self.assertEqual(options['headers']['task'], task_name)
        self.assertEqual(options['routing_key'], queue)
        exchange = options['exchange']
        # Celery's real task sender uses the AMQP default direct exchange for
        # ordinary queue routes. A retried request may retain its named exchange.
        self.assertEqual(getattr(exchange, 'name', exchange), exchange_name)
        self.assertEqual([item.name for item in options['declare']], [queue])
        self.assertEqual(options['declare'][0].exchange.name, queue)
        return options

    def test_legacy_default_and_explicit_routes_publish_to_fixed_queues(self):
        self.assertEqual(self.app.conf.task_default_queue, 'celery')
        self.assertEqual(set(self.app.amqp.queues), {'celery', 'housekeeping'})
        self.assertFalse(self.app.conf.task_create_missing_queues)
        for name in self.book_tasks + self.house_tasks:
            with self.subTest(task=name):
                self.publish.reset_mock()
                self.tasks[name].apply_async(args=('fixture',), ignore_result=True)
                self.assert_envelope(name, 'celery' if name in self.book_tasks else 'housekeeping')

    def test_unknown_future_task_defaults_to_legacy_book_queue(self):
        self.app.send_task('future.unregistered_task', args=('fixture',), ignore_result=True)
        self.assert_envelope('future.unregistered_task', 'celery')

    def test_housekeeping_tasks_get_independent_bounds_not_book_limits(self):
        for name in self.house_tasks:
            with self.subTest(task=name):
                self.publish.reset_mock()
                self.tasks[name].apply_async(ignore_result=True)
                options = self.assert_envelope(name, 'housekeeping')
                self.assertEqual(options['headers']['timelimit'], [1800, 1500])
                self.assertEqual(self.tasks[name].soft_time_limit, 1500)
                self.assertEqual(self.tasks[name].time_limit, 1800)
        self.publish.reset_mock()
        self.tasks['jobs.run_conversion'].apply_async(args=('book', 'attempt'), ignore_result=True)
        self.assertEqual(self.assert_envelope('jobs.run_conversion', 'celery')['headers']['timelimit'], [7500, 7200])
        self.assertEqual(self.app.conf.task_soft_time_limit, 1500)
        self.assertEqual(self.app.conf.task_time_limit, 1800)
        # Chapter experimental task retains the prior global limits (no new
        # annotation overrides it with either housekeeping or whole-book limits).
        self.assertNotIn('jobs.translate_chapter', self.app.conf.task_annotations)

    def test_actual_beat_scheduler_publishes_housekeeping_with_daily_expiration(self):
        from celery.beat import Scheduler, ScheduleEntry
        scheduler = Scheduler(app=self.app, lazy=True)
        for name, spec in self.app.conf.beat_schedule.items():
            with self.subTest(entry=name):
                self.publish.reset_mock()
                entry = ScheduleEntry(name=name, app=self.app, **spec)
                scheduler.apply_async(entry, advance=False)
                options = self.assert_envelope(spec['task'], 'housekeeping')
                self.assertIsNotNone(options['headers']['expires'])
                self.assertEqual(entry.options['expires'], 3600)
                self.assertEqual(entry.options['queue'], 'housekeeping')
                self.assertGreater(float(options['expiration']), 3590)
                self.assertLessEqual(float(options['expiration']), 3600)

    def test_dedicated_conversion_publisher_still_uses_real_book_route(self):
        from app.infra.job_dispatch_publisher import publish_conversion
        from app.domain.dispatch_intent import dispatch_identity
        from kombu import Connection
        app_module = types.ModuleType('app.infra.celery_app')
        task_module = types.ModuleType('app.tasks.job_pipeline')
        app_module.celery_app = self.app
        task_module.run_conversion = self.tasks['jobs.run_conversion']
        with patch.dict(sys.modules, {'app.infra.celery_app': app_module,
                                      'app.tasks.job_pipeline': task_module}), \
             patch.object(Connection, 'ensure_connection', autospec=True) as ensure:
            publish_conversion('book', 'attempt')
        options = self.assert_envelope('jobs.run_conversion', 'celery')
        self.assertEqual(options['headers']['id'], dispatch_identity('book', 'attempt'))
        self.assertTrue(options['headers']['ignore_result'])
        self.assertFalse(options['retry'])
        ensure.assert_called_once()
        self.assertEqual(ensure.call_args.kwargs['max_retries'], 0)

    def test_real_celery_autoretry_preserves_received_book_queue_and_attempt(self):
        from celery.exceptions import Retry
        from app.infra.execution_lease import ExecutionLeaseBusy
        # Use the actual Celery autoretry wrapper around a controlled operation.
        from celery.app.autoretry import add_autoretry_behaviour
        task = self.tasks['jobs.run_conversion']
        def controlled_run(*args, **kwargs):
            raise ExecutionLeaseBusy('Controlled busy lease')
        task.run = controlled_run
        add_autoretry_behaviour(task, autoretry_for=(ExecutionLeaseBusy,),
                               retry_kwargs={'max_retries': 10}, retry_backoff=60,
                               retry_backoff_max=300, retry_jitter=False)
        task.push_request(id='existing-book-message', args=('book', 'attempt-1'), kwargs={},
                          called_directly=False, is_eager=False, retries=0,
                          delivery_info={'exchange': 'celery', 'routing_key': 'celery'})
        try:
            with self.assertRaises(Retry):
                task.run('book', 'attempt-1')
        finally:
            task.pop_request()
        options = self.assert_envelope('jobs.run_conversion', 'celery', exchange_name='celery')
        self.assertEqual(options['headers']['id'], 'existing-book-message')
        self.assertEqual(options['headers']['retries'], 1)
        self.assertEqual(self.publish.call_args.args[1][0], ('book', 'attempt-1'))

    def test_housekeeping_environment_overrides_are_validated_and_isolated(self):
        with patch.dict(os.environ):
            os.environ.pop('CELERY_HOUSEKEEPING_SOFT_TIME_LIMIT', None)
            os.environ.pop('CELERY_HOUSEKEEPING_TIME_LIMIT', None)
            defaults = self.build()
            try:
                self.assertEqual(defaults.conf.task_annotations['jobs.reconcile_payments'],
                                 {'soft_time_limit': 1500, 'time_limit': 1800})
            finally:
                defaults.close()
        with patch.dict(os.environ, {'CELERY_HOUSEKEEPING_SOFT_TIME_LIMIT': '60',
                                     'CELERY_HOUSEKEEPING_TIME_LIMIT': '90'}):
            app = self.build()
            try:
                self.assertEqual(app.conf.task_annotations['infra.health.ping'], {'soft_time_limit': 60, 'time_limit': 90})
                self.assertEqual(app.conf.task_annotations['jobs.run_conversion'], {'soft_time_limit': 7200, 'time_limit': 7500})
            finally:
                app.close()
        for soft, hard in (('0', '180'), ('120', '120'), ('120', '119'), ('-1', '180'),
                           ('x', '180'), ('1.5', '180'), ('120', '3601'), ('3601', '3602')):
            with self.subTest(soft=soft, hard=hard), patch.dict(os.environ, {
                'CELERY_HOUSEKEEPING_SOFT_TIME_LIMIT': soft, 'CELERY_HOUSEKEEPING_TIME_LIMIT': hard,
            }), self.assertRaises(ValueError):
                self.build()

    def test_visibility_timeout_covers_housekeeping_too(self):
        with patch.dict(os.environ, {'EPUB_BOOK_SOFT_TIME_LIMIT': '10', 'EPUB_BOOK_TIME_LIMIT': '20',
                                     'CELERY_TASK_TIME_LIMIT': '30', 'CELERY_VISIBILITY_TIMEOUT': '180',
                                     'CELERY_HOUSEKEEPING_SOFT_TIME_LIMIT': '120',
                                     'CELERY_HOUSEKEEPING_TIME_LIMIT': '180'}), self.assertRaises(ValueError):
            self.build()

    def test_launcher_argv_has_single_role_unique_name_prefork_and_prefetch_one(self):
        self.app.conf.worker_concurrency = 3
        book = self.worker.build_worker_argv('book', config=self.app.conf, pid=123)
        house = self.worker.build_worker_argv('housekeeping', config=self.app.conf, pid=123)
        for argv in (book, house):
            self.assertIn('--pool=prefork', argv)
            self.assertIn('--prefetch-multiplier=1', argv)
            self.assertEqual(len([value for value in argv if value.startswith('--queues=')]), 1)
        self.assertIn('--queues=celery', book)
        self.assertIn('--concurrency=3', book)
        self.assertIn('--queues=housekeeping', house)
        self.assertIn('--concurrency=1', house)
        self.assertIn('--time-limit=1800', house)
        self.assertIn('--soft-time-limit=1500', house)
        self.assertIn('--hostname=fixepub-book-123@%h', book)
        self.assertIn('--hostname=fixepub-housekeeping-123@%h', house)

    def test_launcher_help_and_invalid_role_never_start_worker(self):
        from app.infra.celery_app import celery_app
        with patch.object(celery_app, 'worker_main') as start, redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as caught:
                self.worker.main(['--help'])
            self.assertEqual(caught.exception.code, 0)
            self.assertIn('housekeeping', output.getvalue())
            start.assert_not_called()
        with self.assertRaises(ValueError):
            self.worker.build_worker_argv('both', config=self.app.conf)

    def test_launcher_passes_only_fixed_role_options(self):
        import importlib
        module = importlib.import_module('app.infra.celery_app')
        with patch.object(module, 'celery_app', self.app), patch.object(self.app, 'worker_main', return_value=0) as start:
            self.assertEqual(self.worker.main(['housekeeping', '--loglevel', 'WARNING']), 0)
        self.assertEqual(self.app.conf.epub_worker_role, 'housekeeping')
        argv = start.call_args.args[0]
        self.assertIn('--queues=housekeeping', argv)
        self.assertIn('--loglevel=WARNING', argv)

    def test_signal_rejects_bare_and_mixed_workers_instead_of_swallowing_error(self):
        from celery.signals import worker_init
        sender = type('WorkerStub', (), {'app': self.app, 'concurrency': 1,
                                       'pool_cls': 'prefork', 'options': {}})()
        # Bare WorkController.setup_queues(None) selects both declared queues.
        with self.assertRaises(SystemExit):
            worker_init.send(sender=sender)
        self.app.amqp.queues.select(['celery', 'housekeeping'])
        with self.assertRaises(SystemExit):
            worker_init.send(sender=sender)

    def test_guard_accepts_only_one_supported_queue_and_matching_role(self):
        sender = SimpleNamespace(app=self.app, concurrency=1, pool_cls='prefork', options={})
        self.app.amqp.queues.select(['celery'])
        self.validate(sender)
        self.app.amqp.queues.select(['housekeeping'])
        self.validate(sender)
        sender.concurrency = 2
        with self.assertRaises(SystemExit):
            self.validate(sender)
        sender.concurrency = 1
        self.app.conf.epub_worker_role = 'book'
        with self.assertRaises(SystemExit):
            self.validate(sender)
        self.app.conf.epub_worker_role = 'housekeeping'
        self.validate(sender)

    def test_guard_requires_real_prefork_pool_and_rejects_housekeeping_autoscale(self):
        from celery.concurrency.prefork import TaskPool
        from celery.concurrency.solo import TaskPool as SoloPool
        sender = SimpleNamespace(app=self.app, concurrency=1, pool_cls='prefork', options={})
        for queue in ('celery', 'housekeeping'):
            self.app.amqp.queues.select([queue])
            for pool in ('prefork', 'processes', 'celery.concurrency.prefork:TaskPool', TaskPool):
                with self.subTest(queue=queue, valid_pool=pool):
                    sender.pool_cls = pool
                    self.validate(sender)
            for pool in ('solo', 'gevent', 'eventlet', 'threads', 'custom', SoloPool, None):
                with self.subTest(queue=queue, rejected_pool=pool):
                    sender.pool_cls = pool
                    with self.assertRaisesRegex(SystemExit, 'prefork'):
                        self.validate(sender)
        sender.pool_cls = 'prefork'
        for autoscale in ('3,1', (3, 1), (1, 1)):
            with self.subTest(autoscale=autoscale):
                sender.options = {'autoscale': autoscale}
                with self.assertRaisesRegex(SystemExit, 'autoscale'):
                    self.validate(sender)

    def test_actual_workcontroller_rejects_unsafe_options_before_pool_bootsteps(self):
        from celery.worker.worker import WorkController
        from celery.concurrency.prefork import TaskPool
        # Construct the real controller, but do not start its consumer or pool.
        # Its worker_init signal must stop invalid options before blueprint.apply.
        for queue, options, message in (
            ('housekeeping', {'autoscale': '3,1'}, 'autoscale'),
            ('housekeeping', {'autoscale': (3, 1)}, 'autoscale'),
            ('housekeeping', {'pool': 'solo'}, 'prefork'),
            ('housekeeping', {'pool': 'gevent'}, 'prefork'),
            ('celery', {'pool': 'solo'}, 'prefork'),
        ):
            with self.subTest(queue=queue, options=options), \
                 patch.object(WorkController.Blueprint, 'apply') as bootsteps:
                with self.assertRaisesRegex(SystemExit, message):
                    WorkController(app=self.app, queues=queue, concurrency=1,
                                   **dict({'pool': 'prefork'}, **options))
                bootsteps.assert_not_called()
        for queue in ('celery', 'housekeeping'):
            with self.subTest(valid_queue=queue), \
                 patch.object(WorkController.Blueprint, 'apply') as bootsteps:
                controller = WorkController(app=self.app, queues=queue, concurrency=1, pool='prefork')
                self.assertIs(controller.pool_cls, TaskPool)
                self.assertEqual(controller.concurrency, 1)
                self.assertIsNone(controller.options.get('autoscale'))
                bootsteps.assert_called_once()

    def test_guard_does_not_change_unrelated_celery_app(self):
        from celery import Celery
        unrelated = Celery('other-app', broker='memory://', set_as_current=False)
        try:
            self.validate(SimpleNamespace(app=unrelated, concurrency=5))
            self.assertFalse(unrelated.conf.get('epub_queue_isolation', False))
        finally:
            unrelated.close()


if __name__ == '__main__':
    unittest.main()
