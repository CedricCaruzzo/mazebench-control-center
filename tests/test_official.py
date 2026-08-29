import asyncio
import unittest

from mblab.official import (
    BENCHMARK_DEFAULTS,
    OFFICIAL_HIDDEN_ASCII_INSTRUCTION,
    OFFICIAL_HIDDEN_JSON_INSTRUCTION,
    OFFICIAL_VISIBLE_ASCII_INSTRUCTION,
    OFFICIAL_VISIBLE_JSON_INSTRUCTION,
    base_system_prompt,
    benchmark_contract,
    delivered_system_prompt,
)
from mblab.smoke import ensure_official_system_prompt


class OfficialBenchmarkContractTest(unittest.TestCase):
    def test_hidden_ascii_prompt_restores_native_identity_contract(self):
        base = base_system_prompt()
        delivered = delivered_system_prompt(hide_names=True)

        self.assertTrue(delivered.startswith(base))
        self.assertIn(OFFICIAL_HIDDEN_ASCII_INSTRUCTION, delivered)
        self.assertIn("player P and gem\nG", delivered)
        visible = delivered_system_prompt(hide_names=False)
        self.assertTrue(visible.startswith(base))
        self.assertIn(OFFICIAL_VISIBLE_ASCII_INSTRUCTION, visible)

    def test_json_prompts_restore_native_representation_contract(self):
        hidden = delivered_system_prompt(
            hide_names=True,
            observation_mode="json",
        )
        self.assertIn(OFFICIAL_HIDDEN_JSON_INSTRUCTION, hidden)
        self.assertIn("json_observation.objects", hidden)
        contract = benchmark_contract(
            hide_names=True,
            observation_mode="json",
        )
        self.assertEqual(contract["status"], "passed")
        self.assertEqual(contract["prompt"]["observation_mode"], "json")
        visible = delivered_system_prompt(
            hide_names=False,
            observation_mode="json",
        )
        self.assertIn(OFFICIAL_VISIBLE_JSON_INSTRUCTION, visible)
        self.assertIn("Object type names are literal", visible)

    def test_pinned_official_contract_passes(self):
        contract = benchmark_contract(hide_names=True)

        self.assertEqual(contract["status"], "passed")
        self.assertTrue(all(contract["checks"].values()))
        self.assertEqual(contract["defaults"], BENCHMARK_DEFAULTS)
        self.assertTrue(
            contract["prompt"]["native_hidden_ascii_instruction_applied"]
        )
        self.assertEqual(
            contract["parity_scope"]["agent_protocol"],
            "hosted_multiturn_compatibility_not_native_mcp",
        )

    def test_delivery_repair_inserts_exact_audited_prompt(self):
        class FakeEnvironment:
            async def setup_state(self, state, **kwargs):
                state["prompt"] = [{"role": "user", "content": "observation"}]

        environment = FakeEnvironment()
        expected = delivered_system_prompt(hide_names=True)
        self.assertTrue(ensure_official_system_prompt(environment, expected))

        state = {}
        asyncio.run(environment.setup_state(state))
        self.assertEqual(
            state["prompt"][0],
            {"role": "system", "content": expected},
        )
        self.assertEqual(state["prompt"][1]["role"], "user")


if __name__ == "__main__":
    unittest.main()
