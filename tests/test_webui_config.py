import copy
import unittest

from webui_config import (
    ConfigValidationError,
    load_schema,
    summarize_changes,
    validate_and_normalize,
)


class WebUIConfigTests(unittest.TestCase):
    def setUp(self):
        self.schema = load_schema()
        self.valid = {
            key: copy.deepcopy(field["default"])
            for key, field in self.schema.items()
        }

    def test_complete_round_trip_is_immutable_and_normalized(self):
        candidate = copy.deepcopy(self.valid)
        candidate["sources"][0].update(
            {
                "__template_key": "anything",
                "name": "  Source A  ",
                "keywords": [" ping ", " image "],
                "apis": [" https://example.com/image "],
                "group_list": [" 10001 "],
            }
        )
        original = copy.deepcopy(candidate)
        result = validate_and_normalize(candidate, self.schema)
        self.assertEqual(candidate, original)
        self.assertEqual(result["sources"][0]["name"], "Source A")
        self.assertEqual(result["sources"][0]["keywords"], ["ping", "image"])
        self.assertEqual(result["sources"][0]["apis"], ["https://example.com/image"])
        self.assertEqual(result["sources"][0]["group_list"], ["10001"])
        self.assertEqual(result["sources"][0]["__template_key"], "default_source")

    def test_source_order_is_preserved(self):
        candidate = copy.deepcopy(self.valid)
        second = copy.deepcopy(candidate["sources"][0])
        candidate["sources"][0]["name"] = "first"
        second["name"] = "second"
        candidate["sources"].append(second)
        result = validate_and_normalize(candidate, self.schema)
        self.assertEqual([item["name"] for item in result["sources"]], ["first", "second"])

    def test_template_key_is_optional_and_regenerated(self):
        candidate = copy.deepcopy(self.valid)
        candidate["sources"][0].pop("__template_key")
        result = validate_and_normalize(candidate, self.schema)
        self.assertEqual(result["sources"][0]["__template_key"], "default_source")

    def test_unknown_top_level_keys_are_removed_and_missing_keys_use_defaults(self):
        candidate = copy.deepcopy(self.valid)
        candidate["mystery"] = {"legacy": True}
        candidate.pop("cooldown")

        result = validate_and_normalize(candidate, self.schema)

        self.assertNotIn("mystery", result)
        self.assertEqual(result["cooldown"], self.schema["cooldown"]["default"])

    def test_unknown_source_keys_are_removed_and_missing_source_keys_use_defaults(self):
        candidate = copy.deepcopy(self.valid)
        source = candidate["sources"][0]
        source["legacy_field"] = "old"
        source.pop("recall_delay")
        source.pop("group_list")

        result = validate_and_normalize(candidate, self.schema)

        normalized = result["sources"][0]
        self.assertNotIn("legacy_field", normalized)
        self.assertEqual(normalized["recall_delay"], 0)
        self.assertEqual(normalized["group_list"], [])

    def test_defaults_are_deep_copied_when_fields_are_missing(self):
        first = validate_and_normalize({}, self.schema)
        first["sources"][0]["keywords"].append("mutated")

        second = validate_and_normalize({}, self.schema)

        self.assertNotIn("mutated", second["sources"][0]["keywords"])

    def test_bool_does_not_pass_as_int_and_ranges_apply(self):
        invalid = (("cooldown", True), ("compress_quality", 0), ("compress_quality", 101))
        for field, value in invalid:
            candidate = copy.deepcopy(self.valid)
            candidate[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ConfigValidationError) as caught:
                validate_and_normalize(candidate, self.schema)
            self.assertIn(field, caught.exception.errors)

    def test_enum_url_and_source_requirements(self):
        cases = (
            ("sources[0].list_mode", lambda s: s.update({"list_mode": "other"})),
            ("sources[0].apis[0]", lambda s: s.update({"apis": ["file:///etc/passwd"]})),
            ("sources[0].name", lambda s: s.update({"name": "  "})),
            ("sources[0].keywords", lambda s: s.update({"keywords": ["  "]})),
            ("sources[0].apis", lambda s: s.update({"apis": []})),
        )
        for path, mutation in cases:
            candidate = copy.deepcopy(self.valid)
            mutation(candidate["sources"][0])
            with self.subTest(path=path), self.assertRaises(ConfigValidationError) as caught:
                validate_and_normalize(candidate, self.schema)
            self.assertTrue(any(key == path or key.startswith(path) for key in caught.exception.errors))

    def test_empty_source_list_is_rejected(self):
        candidate = copy.deepcopy(self.valid)
        candidate["sources"] = []
        with self.assertRaises(ConfigValidationError) as caught:
            validate_and_normalize(candidate, self.schema)
        self.assertIn("sources", caught.exception.errors)

    def test_change_summary_reports_scalars_and_source_changes(self):
        before = copy.deepcopy(self.valid)
        before["sources"][0]["name"] = "A"
        second = copy.deepcopy(before["sources"][0])
        second["name"] = "B"
        before["sources"].append(second)
        after = copy.deepcopy(before)
        after["cooldown"] = before["cooldown"] + 1
        after["sources"].reverse()
        summary = summarize_changes(before, after)
        self.assertTrue(any(item["path"] == "cooldown" for item in summary))
        self.assertTrue(any(item["kind"] == "sources_reordered" for item in summary))

        after["sources"].pop()
        after["sources"].append({**copy.deepcopy(second), "name": "C"})
        summary = summarize_changes(before, after)
        self.assertTrue(any(item["kind"] == "source_removed" for item in summary))
        self.assertTrue(any(item["kind"] == "source_added" for item in summary))


if __name__ == "__main__":
    unittest.main()
