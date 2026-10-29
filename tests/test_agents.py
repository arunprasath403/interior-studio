import json
import unittest

from engine import agents, layout
from engine.budget import Preferences, build_bom
from engine.manifest import build_manifest


class Script:
    """A fake LLM that replies from a list and records what it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.asked = []

    def __call__(self, system, user):
        self.asked.append(user)
        return self.replies.pop(0)


GOOD_BRIEF = json.dumps({"budget": 3_000_000, "currency": "inr", "area_sqm": 90, "bhk": "2BHK",
                         "style": "scandinavian", "notes": "no TV"})


def design_reply(rooms, **overrides):
    entry = {"description": "Pale birch floors and soft white walls with warm light.", "exclude": [], "priority": []}
    return json.dumps({"rooms": {r.room_id: {**entry, **overrides.get(r.room_id, {})} for r in rooms}})


class ExtractJsonTests(unittest.TestCase):
    def test_tolerates_fences_and_prose(self):
        self.assertEqual(agents.extract_json('Sure!\n```json\n{"a": 1}\n```\nDone.'), {"a": 1})

    def test_rejects_non_json(self):
        with self.assertRaises(ValueError):
            agents.extract_json("no braces here")


class RequirementsAgentTests(unittest.TestCase):
    def test_parses_a_good_reply(self):
        req = agents.parse_brief(Script(GOOD_BRIEF), "2BHK 90 sqm 30 lakh scandinavian no TV")
        self.assertEqual((req["budget"], req["currency"], req["bhk"]), (3_000_000.0, "INR", "2BHK"))

    def test_retries_with_the_rejection_reason(self):
        llm = Script("not json at all", GOOD_BRIEF)
        agents.parse_brief(llm, "brief")
        self.assertEqual(len(llm.asked), 2)
        self.assertIn("rejected", llm.asked[1])

    def test_gives_up_after_the_attempt_limit(self):
        llm = Script(*["garbage"] * agents.MAX_ATTEMPTS)
        with self.assertRaises(agents.AgentError):
            agents.parse_brief(llm, "brief")
        self.assertEqual(len(llm.asked), agents.MAX_ATTEMPTS)

    def test_missing_information_is_reported_without_retrying(self):
        llm = Script(json.dumps({"budget": None, "area_sqm": 90, "bhk": "2BHK"}))
        with self.assertRaises(agents.AgentError) as caught:
            agents.parse_brief(llm, "a nice 2BHK")
        self.assertIn("budget", str(caught.exception))
        self.assertEqual(len(llm.asked), 1)

    def test_impossible_area_is_reported_without_retrying(self):
        llm = Script(json.dumps({"budget": 1000, "area_sqm": 12, "bhk": "3BHK"}))
        with self.assertRaises(agents.AgentError):
            agents.parse_brief(llm, "tiny 3BHK")
        self.assertEqual(len(llm.asked), 1)

    def test_negative_budget_is_rejected_and_retried(self):
        bad = json.dumps({"budget": -5, "area_sqm": 90, "bhk": "2BHK"})
        req = agents.parse_brief(Script(bad, GOOD_BRIEF), "brief")
        self.assertEqual(req["budget"], 3_000_000.0)


class DesignAgentTests(unittest.TestCase):
    def setUp(self):
        self.rooms = layout.visible_rooms(layout.allocate(90, "2BHK"))

    def test_accepts_a_valid_plan(self):
        out = agents.design_rooms(Script(design_reply(self.rooms)), self.rooms, "scandinavian", "")
        self.assertEqual(set(out), {r.room_id for r in self.rooms})

    def test_unknown_item_is_rejected_then_corrected(self):
        first = design_reply(self.rooms, **{self.rooms[0].room_id: {"exclude": ["flux_capacitor"]}})
        llm = Script(first, design_reply(self.rooms))
        agents.design_rooms(llm, self.rooms, "x", "")
        self.assertEqual(len(llm.asked), 2)
        self.assertIn("flux_capacitor", llm.asked[1])

    def test_unknown_room_is_rejected(self):
        bad = json.dumps({"rooms": {"room_attic_99": {"description": "x"}}})
        with self.assertRaises(agents.AgentError):
            agents.design_rooms(Script(*[bad] * agents.MAX_ATTEMPTS), self.rooms, "x", "")

    def test_overlong_description_is_rejected(self):
        bad = design_reply(self.rooms, **{self.rooms[0].room_id: {"description": "x" * 500}})
        with self.assertRaises(agents.AgentError):
            agents.design_rooms(Script(*[bad] * agents.MAX_ATTEMPTS), self.rooms, "x", "")

    def test_cannot_gut_a_room(self):
        living = next(r for r in self.rooms if r.kind == "living")
        everything = [i for i, _ in agents.catalog_options("living")]
        bad = design_reply(self.rooms, **{living.room_id: {"exclude": everything}})
        with self.assertRaises(agents.AgentError):
            agents.design_rooms(Script(*[bad] * agents.MAX_ATTEMPTS), self.rooms, "x", "")


class PlanFromBriefTests(unittest.TestCase):
    def rooms(self):
        return layout.visible_rooms(layout.allocate(90, "2BHK"))

    def test_end_to_end_applies_the_design(self):
        rooms = self.rooms()
        living = next(r for r in rooms if r.kind == "living")
        llm = Script(GOOD_BRIEF, design_reply(rooms, **{living.room_id: {"exclude": ["tv_unit"],
                                                                         "description": "Birch floor, linen sofa."}}))
        manifest = agents.plan_from_brief(llm, "brief")
        node = next(n for n in manifest["topology"]["nodes"] if n["room_id"] == living.room_id)
        titles = [h["payload"]["title"] for h in node["hotspots"] if h["type"] == "product_tag"]
        self.assertNotIn("Wall TV Console", titles)
        self.assertIn("Birch floor, linen sofa.", node["diffusion_parameters"]["positive_prompt"])
        self.assertEqual(manifest["metadata"]["design_source"], "agent")
        self.assertLessEqual(manifest["metadata"]["spent"], manifest["metadata"]["total_budget"])

    def test_falls_back_to_rules_when_design_fails(self):
        llm = Script(GOOD_BRIEF, *["garbage"] * agents.MAX_ATTEMPTS)
        manifest = agents.plan_from_brief(llm, "brief")
        self.assertEqual(manifest["metadata"]["design_source"], "rules")
        self.assertLessEqual(manifest["metadata"]["spent"], manifest["metadata"]["total_budget"])


class BudgetCeilingHoldsAgainstAnyPlanTests(unittest.TestCase):
    """The model is untrusted: whatever it proposes, the ceiling must hold."""

    def test_excluding_and_reordering_never_exceeds_the_budget(self):
        rooms = layout.allocate(90, "2BHK")
        for budget in (4_000, 40_000, 400_000, 4_000_000):
            for exclude_n in (0, 2, 4):
                prefs = Preferences(
                    priorities={r.room_id: [i for i, _ in reversed(agents.catalog_options(r.kind))] for r in rooms},
                    excludes={r.room_id: frozenset(i for i, _ in agents.catalog_options(r.kind)[:exclude_n]) for r in rooms},
                )
                items, _ = build_bom(rooms, budget, 90, prefs)
                self.assertLessEqual(sum(i.cost for i in items), budget, (budget, exclude_n))

    def test_excluding_a_whole_category_does_not_crash(self):
        rooms = layout.allocate(90, "2BHK")
        lighting = {r.room_id: frozenset(i for i, _ in agents.catalog_options(r.kind)) for r in rooms}
        items, _ = build_bom(rooms, 40_000, 90, Preferences(excludes=lighting))
        self.assertLessEqual(sum(i.cost for i in items), 40_000)

    def test_excluded_items_free_money_for_the_rest(self):
        full = build_manifest(40_000, 90, "2BHK", "x")["metadata"]["spent"]
        rooms = layout.allocate(90, "2BHK")
        no_tv = {r.room_id: {"exclude": ["tv_unit"]} for r in rooms if r.kind == "living"}
        trimmed = build_manifest(40_000, 90, "2BHK", "x", design=no_tv)["metadata"]["spent"]
        self.assertGreater(trimmed, 0.9 * full)  # the budget moves to other pieces rather than going unspent


if __name__ == "__main__":
    unittest.main()
