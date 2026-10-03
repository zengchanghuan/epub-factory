"""Offline PDF product boundary tests; parser/validator transport is controlled."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.cancellation import JobCancelled
from app.domain import pdf_product as product


SOURCE = b'%PDF-1.4 controlled source identity\n'
ARTIFACT = b'controlled validated EPUB bytes, never parsed by these product tests'


def versions():
    return {key: ('a' * 64 if key.endswith('_sha256') else 'test-1.0.0') for key in product._VERSION_KEYS}


def report(warnings=(), *, memory=True):
    return {'schema_version': 'pdf-text-epub-v1', 'source_sha256': hashlib.sha256(SOURCE).hexdigest(),
            'output_sha256': hashlib.sha256(ARTIFACT).hexdigest(), 'page_count': 2,
            'normalized_characters': 123, 'zero_width_spaces_preserved': 4,
            'image_assets': 1, 'image_placements': 1, 'toc_entries': 2, 'paragraph_count': 5,
            'warnings': list(warnings), 'memory_limited': memory, 'validation_passed': True,
            'epubcheck_warnings': 0, 'requires_review': bool(warnings), 'eligible_for_payment': not bool(warnings)}


def pdf_job(source, *, identity='pdf-order', plan=None):
    return SimpleNamespace(id=identity, input_path=str(source), source_filename='Original source.pdf',
        output_mode='original', enable_translation=False, enable_precision_polish=False, bilingual=False,
        status='awaiting_confirm',
        batch_id='', expected_amount='0.99', is_test_order=False, payment_entitlement={}, payment_resolution={},
        translation_stats={'attempt_id': 'prepare-attempt', 'pdf_conversion': plan or product.new_pdf_plan(
            hashlib.sha256(SOURCE).hexdigest(), len(SOURCE), '0.99')})


class PdfProductTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='pdf-product-')))
        self.source, self.output = self.root / 'source.pdf', self.root / 'output.epub'
        self.source.write_bytes(SOURCE)
        self.artifacts = self.root / 'artifacts'; self.artifacts.mkdir()
        self.job = pdf_job(self.source)
        self.network = [self.stack.enter_context(patch.object(socket.socket, name, side_effect=AssertionError('No network')))
                        for name in ('connect', 'connect_ex', 'sendto', 'sendmsg')]
        self.network.append(self.stack.enter_context(patch.object(socket, 'getaddrinfo', side_effect=AssertionError('No DNS'))))
        self.addCleanup(lambda: [mock.assert_not_called() for mock in self.network])
        self.stack.enter_context(patch.object(product, '_versions', side_effect=lambda jar: versions()))
        self.parser = self.stack.enter_context(patch.object(product.pdf_conversion, 'convert_text_pdf', side_effect=self.convert))
        self.receipt = report()

    def convert(self, source, destination, **kwargs):
        self.assertEqual(Path(source).read_bytes(), SOURCE)
        Path(destination).write_bytes(ARTIFACT)
        return deepcopy(self.receipt)

    def prepared(self, **kwargs):
        plan = product.prepare_pdf_artifact(self.job, self.artifacts, **kwargs)
        self.job.translation_stats['pdf_conversion'] = plan
        return plan

    def confirmed(self, warnings=()):
        plan = self.prepared()
        plan = product.confirm_pdf_plan(self.job, self.artifacts, plan['plan_id'], list(warnings))
        self.job.translation_stats['pdf_conversion'] = plan
        return plan

    def pay(self):
        self.job.payment_resolution = {'state': 'paid', 'source': 'verified_webhook', 'amount': '0.99'}

    def test_private_preparation_is_frozen_without_output_or_payment(self):
        plan = self.prepared()
        self.assertEqual(plan['phase'], 'prepared')
        self.assertEqual(plan['plan_id'], product.pdf_plan_identity(plan))
        self.assertFalse(self.output.exists())
        self.assertEqual(self.job.payment_resolution, {})
        self.assertEqual(self.source.read_bytes(), SOURCE)
        artifact = product._artifact_path(plan, self.artifacts)
        self.assertEqual(artifact.read_bytes(), ARTIFACT)
        self.assertEqual(artifact.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(product.validate_pdf_job(self.job, self.artifacts), plan)

    def test_known_warnings_must_be_exactly_acknowledged_without_mutating_report(self):
        warnings = sorted(product.ACKNOWLEDGEABLE_WARNINGS)
        self.receipt = report(warnings)
        plan = self.prepared()
        for accepted in ([], warnings[:-1], warnings + ['unknown'], warnings + warnings[:1]):
            with self.subTest(count=len(accepted)), self.assertRaises(product.PdfProductError):
                product.confirm_pdf_plan(self.job, self.artifacts, plan['plan_id'], accepted)
        confirmed = product.confirm_pdf_plan(self.job, self.artifacts, plan['plan_id'], warnings)
        self.assertEqual(confirmed['report'], plan['report'])
        self.assertTrue(confirmed['report']['requires_review'])
        self.assertFalse(confirmed['report']['eligible_for_payment'])
        self.assertEqual(confirmed['plan_id'], plan['plan_id'])

    def test_hard_memory_absence_and_epub_warnings_cannot_be_acknowledged(self):
        for warning in ('memory_limit_unavailable', 'epubcheck_warnings'):
            with self.subTest(warning=warning):
                self.job = pdf_job(self.source)
                self.receipt = report([warning], memory=warning != 'memory_limit_unavailable')
                self.receipt['epubcheck_warnings'] = int(warning == 'epubcheck_warnings')
                plan = self.prepared()
                with self.assertRaises(product.PdfProductError) as caught:
                    product.confirm_pdf_plan(self.job, self.artifacts, plan['plan_id'], [warning])
                self.assertEqual(caught.exception.reason, 'review_blocked')
                self.assertFalse(product.public_pdf_summary(self.job)['can_confirm'])

    def test_unknown_warning_or_manuscript_field_cannot_enter_plan(self):
        for extra in ({'warnings': ['private manuscript content']}, {'page_count': True}):
            with self.subTest(field=next(iter(extra))), self.assertRaises(product.PdfProductError):
                self.receipt = {**report(), **extra}
                self.prepared()

    def test_digest_detects_changes_and_rejects_extra_private_path(self):
        plan = self.prepared()
        for change in ({'amount': '1.99'}, {'artifact_id': 'b' * 32}, {'source_bytes': 10}, {'path': '/private/source'}):
            with self.subTest(field=next(iter(change))), self.assertRaises(product.PdfProductError):
                product.validate_pdf_plan({**plan, **change})

    def test_invalid_phase_and_numeric_types_raise_safe_product_error(self):
        plan = self.job.translation_stats['pdf_conversion']
        for change in ({'phase': []}, {'source_bytes': True}, {'amount': 'NaN'}, {'schema_version': True}, {'source_sha256': '../escape'}):
            with self.subTest(field=next(iter(change))), self.assertRaises(product.PdfProductError):
                product.validate_pdf_plan({**plan, **change})

    def test_plan_validation_returns_detached_data(self):
        plan = self.prepared()
        copy = product.validate_pdf_plan(plan)
        copy['report']['warnings'].append('new')
        self.assertEqual(plan['report']['warnings'], [])

    def test_unsupported_combinations_never_run_parser(self):
        for change in ({'output_mode': 'simplified'}, {'enable_translation': True}, {'enable_precision_polish': True},
                       {'bilingual': True}, {'batch_id': 'batch'}, {'expected_amount': '1.99'}):
            with self.subTest(field=next(iter(change))):
                job = pdf_job(self.source); job.__dict__.update(change)
                with self.assertRaises(product.PdfProductError):
                    product.prepare_pdf_artifact(job, self.artifacts)
        self.parser.assert_not_called()

    def test_source_changed_before_preparation_or_confirmation_is_rejected(self):
        self.source.write_bytes(SOURCE + b'changed')
        with self.assertRaises(product.PdfProductError):
            self.prepared()
        self.parser.assert_not_called()
        self.source.write_bytes(SOURCE)
        plan = self.prepared()
        self.source.write_bytes(SOURCE + b'changed')
        with self.assertRaises(product.PdfProductError):
            product.confirm_pdf_plan(self.job, self.artifacts, plan['plan_id'], [])

    def test_source_symlink_and_artifact_symlink_are_rejected(self):
        alias = self.root / 'alias.pdf'; alias.symlink_to(self.source)
        job = pdf_job(alias)
        with self.assertRaises(product.PdfProductError):
            product.prepare_pdf_artifact(job, self.artifacts)
        plan = self.prepared()
        artifact = product._artifact_path(plan, self.artifacts)
        saved = artifact.with_name('saved.epub'); artifact.rename(saved); artifact.symlink_to(saved)
        with self.assertRaises(product.PdfProductError):
            product.validate_pdf_job(self.job, self.artifacts)

    def test_symlink_storage_root_rejected_without_writes(self):
        alias = self.root / 'alias'; alias.symlink_to(self.artifacts, target_is_directory=True)
        with self.assertRaises(product.PdfProductError):
            product.prepare_pdf_artifact(self.job, alias)
        self.assertEqual(list(self.artifacts.iterdir()), [])
        self.parser.assert_not_called()

    def test_changed_artifact_cannot_confirm_or_deliver(self):
        plan = self.confirmed(); self.pay()
        product._artifact_path(plan, self.artifacts).write_bytes(b'changed')
        with self.assertRaises(product.PdfProductError):
            product.copy_prepared_pdf(self.job, self.output, self.artifacts)
        self.assertFalse(self.output.exists())

    def test_confirmed_paid_delivery_is_exact_and_does_not_reparse(self):
        plan = self.confirmed(); self.pay()
        result = product.copy_prepared_pdf(self.job, self.output, self.artifacts)
        self.assertEqual(self.output.read_bytes(), ARTIFACT)
        self.assertEqual(result.translation_stats['pdf_conversion'], plan)
        self.assertEqual(self.parser.call_count, 1)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        product.validate_pdf_delivery(self.job, self.output)

    def test_status_and_test_order_flag_are_not_payment_proof(self):
        self.confirmed(); self.job.status = 'success'; self.job.is_test_order = True
        for proof in ({}, {'state': 'paid', 'source': 'browser', 'amount': '0.99'},
                      {'state': 'paid', 'source': 'verified_webhook', 'amount': '1.99'}):
            self.job.payment_resolution = proof
            with self.assertRaises(product.PdfProductError) as caught:
                product.copy_prepared_pdf(self.job, self.output, self.artifacts)
            self.assertEqual(caught.exception.reason, 'payment_required')
        self.assertFalse(self.output.exists())

    def test_strict_server_test_entitlement_allows_delivery(self):
        self.confirmed(); self.job.is_test_order = True
        self.job.payment_entitlement = {'state': 'test_authorized', 'source': 'server_test_bypass',
                                       'order_no': self.job.id, 'amount': '0.99'}
        product.copy_prepared_pdf(self.job, self.output, self.artifacts)
        self.assertEqual(self.output.read_bytes(), ARTIFACT)

    def test_refund_recorded_blocks_even_previously_paid_delivery(self):
        self.confirmed(); self.pay(); self.job.payment_resolution['refund_recorded'] = True
        with self.assertRaises(product.PdfProductError):
            product.copy_prepared_pdf(self.job, self.output, self.artifacts)
        self.assertFalse(self.output.exists())

    def test_download_validation_does_not_need_cleaned_original_or_prepared_file(self):
        plan = self.confirmed(); self.pay()
        product.copy_prepared_pdf(self.job, self.output, self.artifacts)
        self.source.unlink(); product._artifact_path(plan, self.artifacts).unlink()
        product.validate_pdf_delivery(self.job, self.output)
        self.output.write_bytes(b'changed')
        with self.assertRaises(product.PdfProductError):
            product.validate_pdf_delivery(self.job, self.output)

    def test_existing_destination_is_not_overwritten(self):
        self.confirmed(); self.pay(); self.output.write_bytes(b'existing')
        with self.assertRaises(product.PdfProductError):
            product.copy_prepared_pdf(self.job, self.output, self.artifacts)
        self.assertEqual(self.output.read_bytes(), b'existing')

    def test_cancelled_copy_after_link_rolls_back_owned_output(self):
        self.confirmed(); self.pay()
        cancelled = False
        original = os.fsync
        def sync(fd):
            nonlocal cancelled
            result = original(fd)
            if self.output.exists():
                cancelled = True
            return result
        with patch.object(product.os, 'fsync', side_effect=sync), self.assertRaises(JobCancelled):
            product.copy_prepared_pdf(self.job, self.output, self.artifacts, cancel_check=lambda: cancelled)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob('.pdf-copy-*')))

    def test_cancel_before_preparation_has_no_parser_or_artifact(self):
        with self.assertRaises(JobCancelled):
            self.prepared(cancel_check=lambda: True)
        self.parser.assert_not_called()
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_public_summary_never_contains_private_descriptor_or_paths(self):
        self.prepared()
        summary = product.public_pdf_summary(self.job)
        self.assertEqual(set(summary), {'phase', 'plan_id', 'amount', 'page_count', 'normalized_characters',
            'image_assets', 'toc_entries', 'warnings', 'can_confirm', 'blocked_reason', 'preserves_original', 'confirmed'})
        self.assertNotIn('artifact', repr(summary)); self.assertNotIn(str(self.root), repr(summary))
        self.assertTrue(summary['can_confirm'])
        self.job.status = 'cancelled'
        self.assertFalse(product.public_pdf_summary(self.job)['can_confirm'])

    def test_post_link_interrupt_removes_own_artifact_but_not_another_writers_inode(self):
        self.confirmed(); self.pay()
        original = os.fsync
        for replace in (False, True):
            with self.subTest(replacement=replace):
                replacement = self.root / 'replacement.epub'
                if replace:
                    replacement.write_bytes(b'other writer')
                def interrupt(fd):
                    result = original(fd)
                    if self.output.exists():
                        if replace:
                            replacement.replace(self.output)
                        raise KeyboardInterrupt()
                    return result
                with patch.object(product.os, 'fsync', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
                    product.copy_prepared_pdf(self.job, self.output, self.artifacts)
                if replace:
                    self.assertEqual(self.output.read_bytes(), b'other writer')
                else:
                    self.assertFalse(self.output.exists())
                self.assertFalse(list(self.root.glob('.pdf-copy-*')))

    def test_versions_changing_during_preparation_cannot_be_frozen(self):
        changed = {**versions(), 'parser_sha256': 'b' * 64}
        with patch.object(product, '_versions', side_effect=[versions(), changed]), self.assertRaises(product.PdfProductError):
            self.prepared()
        self.assertEqual(self.job.translation_stats['pdf_conversion']['phase'], 'preparing')
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
