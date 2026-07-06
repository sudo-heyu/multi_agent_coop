import json
import os
import unittest
from unittest.mock import MagicMock, patch

from src.memory import llm_backend


class MemoryLlmBackendTests(unittest.TestCase):
    def test_ppio_openai_request_and_response(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "云端总结"}}]
        }).encode()
        response.__exit__.return_value = False
        with patch.dict(os.environ, {
            "PPIO_API_KEY": "test-key",
            "MULTIAP_MEMORY_PPIO_MODEL": "strong/model",
        }), patch("urllib.request.urlopen", return_value=response) as urlopen:
            result = llm_backend.summarize("证据")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode())
        self.assertEqual(result, "云端总结")
        self.assertEqual(payload["model"], "strong/model")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertTrue(request.full_url.endswith("/chat/completions"))

    def test_missing_key_fails_without_local_model_fallback(self):
        with patch.dict(os.environ, {"PPIO_API_KEY": ""}, clear=False), patch.object(
            llm_backend.Path, "read_text", side_effect=OSError
        ):
            with self.assertRaisesRegex(RuntimeError, "PPIO_API_KEY"):
                llm_backend.summarize("证据")


if __name__ == "__main__":
    unittest.main()
