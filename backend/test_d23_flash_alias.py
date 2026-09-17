"""Offline model-name migration checks; never calls a provider."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

_runtime = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL'] = 'sqlite:///' + _runtime.name + '/jobs.db'
os.environ['OPENAI_API_KEY'] = 'offline-only'
os.environ['ALIPAY_APP_ID'] = ''

from app.main import _normalize_translation_model
from app.models import Job, OutputMode
from app.infra.llm_guard import assert_model_allowed, ModelNotAllowedError
from app.engine.cleaners.semantics_translator import SemanticsTranslator

FLASH_ALIASES = ('deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp')


class FlashAliasTests(unittest.TestCase):
    def test_new_defaults_use_stable_official_name(self):
        with patch('app.main.DEFAULT_TRANSLATION_MODEL', 'deepseek-flash'):
            for quality in ('standard', 'high', 'literary'):
                self.assertEqual(_normalize_translation_model(None, True, translation_quality=quality), 'deepseek-flash')
        job = Job(id='offline', trace_id='offline', source_filename='fixture.epub',
                  input_path='fixture.epub', output_mode=OutputMode.simplified)
        self.assertEqual(job.translation_model, 'deepseek-flash')

    def test_explicit_historical_names_are_not_rewritten(self):
        for name in FLASH_ALIASES:
            self.assertEqual(_normalize_translation_model(name, True), name)
            with patch.dict(os.environ, {}, clear=True):
                assert_model_allowed(name)
        self.assertEqual(_normalize_translation_model('deepseek-v4-pro', True), 'deepseek-v4-pro')

    def test_custom_flash_allowlist_understands_only_documented_aliases(self):
        for configured in FLASH_ALIASES:
            with patch.dict(os.environ, {'LLM_MODEL_ALLOWLIST': configured}, clear=True):
                for name in FLASH_ALIASES:
                    assert_model_allowed(name)
                with self.assertRaises(ModelNotAllowedError):
                    assert_model_allowed('deepseek-v4.5-pro')
        with patch.dict(os.environ, {'LLM_MODEL_ALLOWLIST': 'gpt-4o-mini'}, clear=True):
            with self.assertRaises(ModelNotAllowedError):
                assert_model_allowed('deepseek-flash')

    def test_translator_defaults_and_historical_cache_identity(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'offline-only'}, clear=True), \
             patch('app.engine.cleaners.semantics_translator.TranslationCache', return_value=Mock()):
            current = SemanticsTranslator(target_lang='zh-CN')
            historical = SemanticsTranslator(target_lang='zh-CN', model='deepseek-v4-flash')
        self.assertEqual(current.model, 'deepseek-flash')
        self.assertEqual(historical.model, 'deepseek-v4-flash')
        self.assertIn('@deepseek-v4-flash@', historical._cache_family_key)
        # Verified caches remain model-version specific; do not mislabel older V4 translations.
        self.assertNotEqual(current._cache_family_key, historical._cache_family_key)

    def test_deployment_and_frontend_defaults_match(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn("'OPENAI_MODEL': 'deepseek-flash'", (root / 'scripts/deploy-server.sh').read_text())
        self.assertIn('OPENAI_MODEL=deepseek-flash', (root / 'backend/.env.example').read_text())
        html = (root / 'frontend/index.html').read_text()
        self.assertIn('id="translationModel" value="deepseek-flash"', html)
        self.assertIn('DeepSeek V4.1 Flash', html)


if __name__ == '__main__':
    unittest.main()
