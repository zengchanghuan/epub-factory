"""Exercise real Celery/Kombu publication construction without network IO."""
import socket
import sys
import types
import unittest
from unittest.mock import patch

from celery import Celery
from celery.backends.redis import RedisBackend
from kombu import Connection, Producer
from kombu.exceptions import OperationalError
from kombu.pools import ProducerPool

from app.domain.dispatch_intent import dispatch_identity
from app.infra.job_dispatch_publisher import publish_conversion


class DispatchPublisherTests(unittest.TestCase):
    def setUp(self):
        self.app = Celery('dispatch-contract-test', broker='redis://127.0.0.1:6379/10',
                          backend='redis://127.0.0.1:6379/11', set_as_current=False)
        self.addCleanup(self.app.close)
        self.original_options = {'visibility_timeout': 15000, 'socket_connect_timeout': 77,
                                 'socket_timeout': 88, 'retry_on_timeout': True, 'health_check_interval': 30}
        self.app.conf.update(broker_transport_options=dict(self.original_options),
                             broker_connection_timeout=99, task_always_eager=False)

        @self.app.task(name='jobs.run_conversion', shared=False)
        def task(job_id, expected_attempt_id):
            raise AssertionError('Publisher must never execute task inline')
        self.task = task._get_current_object()
        app_module, task_module = types.ModuleType('app.infra.celery_app'), types.ModuleType('app.tasks.job_pipeline')
        app_module.celery_app = self.app
        task_module.run_conversion = self.task
        modules = patch.dict(sys.modules, {'app.infra.celery_app': app_module, 'app.tasks.job_pipeline': task_module})
        modules.start(); self.addCleanup(modules.stop)

        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('No broker/backend network'))
        self.network = network.start(); self.addCleanup(network.stop)
        backend = patch.object(RedisBackend, 'on_task_call', side_effect=AssertionError('Result backend subscription prohibited'))
        self.backend_call = backend.start(); self.addCleanup(backend.stop)
        # Celery reads pool connection metadata to identify its driver even
        # when a dedicated connection is supplied. Reading is not acquiring.
        pool = patch.object(ProducerPool, 'acquire',
                            side_effect=AssertionError('Publisher must not acquire pooled producer'))
        self.pool = pool.start(); self.addCleanup(pool.stop)

        # These are the lowest transport boundaries: connection construction,
        # task.apply_async, task-message generation and Producer creation stay
        # real. Only connection establishment / transport send are replaced.
        establish = patch.object(Connection, 'ensure_connection', autospec=True)
        self.ensure = establish.start(); self.addCleanup(establish.stop)
        send = patch.object(Producer, 'publish', autospec=True)
        self.send = send.start(); self.addCleanup(send.stop)
        original_release = Connection.release
        release = patch.object(Connection, 'release', autospec=True, side_effect=original_release)
        self.release = release.start(); self.addCleanup(release.stop)
        apply = patch.object(self.task, 'apply_async', wraps=self.task.apply_async)
        self.apply = apply.start(); self.addCleanup(apply.stop)

    def test_real_dedicated_connection_has_bounded_local_options(self):
        publish_conversion('job-123', 'attempt-2')
        self.ensure.assert_called_once()
        connection = self.ensure.call_args.args[0]
        self.assertIsInstance(connection, Connection)
        self.assertEqual(self.ensure.call_args.kwargs, {'max_retries': 0})
        self.assertEqual(connection.connect_timeout, 5)
        self.assertEqual(connection.transport_options, {
            **self.original_options, 'socket_connect_timeout': 5, 'socket_timeout': 5, 'retry_on_timeout': False,
        })
        self.assertEqual(self.app.conf.broker_transport_options, self.original_options)
        self.assertEqual(self.app.conf.broker_connection_timeout, 99)
        self.release.assert_called_once_with(connection)
        self.assertTrue(connection._closed)
        self.network.assert_not_called()
        self.pool.assert_not_called()

    def test_real_apply_async_disables_retry_and_backend_subscription(self):
        publish_conversion('job-123', 'attempt-2')
        self.apply.assert_called_once_with(
            args=('job-123', 'attempt-2'), connection=self.ensure.call_args.args[0], retry=False,
            task_id=dispatch_identity('job-123', 'attempt-2'), ignore_result=True,
        )
        self.send.assert_called_once()
        kwargs = self.send.call_args.kwargs
        self.assertIs(kwargs['retry'], False)
        self.assertEqual(kwargs['headers']['id'], dispatch_identity('job-123', 'attempt-2'))
        self.assertEqual(kwargs['headers']['task'], 'jobs.run_conversion')
        self.assertIs(kwargs['headers']['ignore_result'], True)
        self.assertEqual(self.send.call_args.args[1][0], ('job-123', 'attempt-2'))
        self.backend_call.assert_not_called()
        self.network.assert_not_called()

    def test_identity_is_stable_per_attempt_and_distinguishes_boundaries(self):
        pairs = [('job', ''), ('job', ''), ('job', 'attempt-2'), ('a:b', 'c'), ('a', 'b:c')]
        for job, attempt in pairs:
            publish_conversion(job, attempt)
        ids = [call.kwargs['task_id'] for call in self.apply.call_args_list]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(len(set(ids)), 4)
        self.assertTrue(all(len(identity) == 64 for identity in ids))
        connections = [call.args[0] for call in self.ensure.call_args_list]
        self.assertEqual(len({id(connection) for connection in connections}), len(pairs))
        self.assertEqual(self.release.call_count, len(pairs))

    def test_connect_error_propagates_without_publish_and_closes_resource(self):
        failure = OperationalError('controlled unavailable broker')
        self.ensure.side_effect = failure
        with self.assertRaises(OperationalError) as caught:
            publish_conversion('job', 'attempt')
        self.assertIs(caught.exception, failure)
        self.apply.assert_not_called()
        self.send.assert_not_called()
        self.ensure.assert_called_once()
        self.release.assert_called_once_with(self.ensure.call_args.args[0])
        self.network.assert_not_called()

    def test_transport_publish_error_propagates_without_retry_and_closes(self):
        failure = OperationalError('controlled transport failure')
        self.send.side_effect = failure
        with self.assertRaises(OperationalError) as caught:
            publish_conversion('job', 'attempt')
        self.assertIs(caught.exception, failure)
        self.ensure.assert_called_once()
        self.send.assert_called_once()
        self.assertIs(self.send.call_args.kwargs['retry'], False)
        self.release.assert_called_once_with(self.ensure.call_args.args[0])
        self.backend_call.assert_not_called()
        self.network.assert_not_called()

    def test_message_construction_failure_also_releases_connection(self):
        self.apply.side_effect = ValueError('controlled serialization failure')
        with self.assertRaisesRegex(ValueError, 'controlled serialization failure'):
            publish_conversion('job', '')
        self.send.assert_not_called()
        self.release.assert_called_once_with(self.ensure.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
