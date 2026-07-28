import copy
import unittest

from executor.bench import validate


class ValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = validate.load_json(validate.MANIFEST_PATH)
        cls.scenarios = validate.validate_manifest(cls.manifest)
        cls.fixture = validate.load_json(validate.FIXTURE_PATH)

    def test_synthetic_fixture_is_valid_but_not_evidence(self):
        validate.validate_result(copy.deepcopy(self.fixture), self.scenarios)
        self.assertEqual(self.fixture["overall_acceptance"]["verdict"], "NOT_EVALUATED")

    def test_instant_dependency_use_fails(self):
        result = copy.deepcopy(self.fixture)
        warm = next(item for item in result["scenario_results"] if item["scenario_id"] == "warm_invocation_startup")
        warm["instant_path_proof"]["dependency_operations"]["redis"] = 1
        with self.assertRaisesRegex(validate.ContractError, "instant path used redis"):
            validate.validate_result(result, self.scenarios)

    def test_synthetic_fixture_cannot_claim_pass(self):
        result = copy.deepcopy(self.fixture)
        result["overall_acceptance"]["verdict"] = "PASS"
        with self.assertRaisesRegex(validate.ContractError, "overall verdict must be NOT_EVALUATED"):
            validate.validate_result(result, self.scenarios)

    def test_invalid_source_sha_fails(self):
        result = copy.deepcopy(self.fixture)
        result["run"]["source_sha"] = "unverified"
        with self.assertRaisesRegex(validate.ContractError, "source_sha"):
            validate.validate_result(result, self.scenarios)

    def test_same_timestamp_does_not_prove_preparation_precedes_first_call(self):
        result = copy.deepcopy(self.fixture)
        prepare = next(item for item in result["scenario_results"] if item["scenario_id"] == "prepare_before_first_call")
        prepare["event_timeline"][-1]["monotonic_ms"] = 3
        with self.assertRaisesRegex(validate.ContractError, "preparation timeline"):
            validate.validate_result(result, self.scenarios)


if __name__ == "__main__":
    unittest.main()
