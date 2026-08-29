import unittest

from mblab.llamacpp_compat import clean_native_messages


class LlamaCppCompatibilityTest(unittest.TestCase):
    def test_reasoning_only_assistant_gets_empty_content(self):
        messages = clean_native_messages(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": None,
                    "reasoning_content": "I should move down.",
                }
            ]
        )
        self.assertEqual(
            messages,
            [
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "I should move down.",
                }
            ],
        )

    def test_normal_assistant_content_is_unchanged(self):
        messages = clean_native_messages(
            [{"role": "assistant", "content": "down", "tool_calls": None}]
        )
        self.assertEqual(messages, [{"role": "assistant", "content": "down"}])


if __name__ == "__main__":
    unittest.main()
