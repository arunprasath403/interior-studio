import unittest

from engine import layout
from engine.budget import build_bom
from engine.manifest import build_manifest
from engine.prompts import seed_for


class LayoutTests(unittest.TestCase):
    def test_areas_sum_to_total(self):
        rooms = layout.allocate(90, "2BHK")
        self.assertAlmostEqual(sum(r.area_sqm for r in rooms), 90, delta=0.1)

    def test_room_counts_follow_bhk(self):
        kinds = [r.kind for r in layout.allocate(120, "3BHK")]
        self.assertEqual(kinds.count("master_bedroom"), 1)
        self.assertEqual(kinds.count("bedroom"), 2)

    def test_too_small_area_is_rejected(self):
        with self.assertRaises(ValueError):
            layout.allocate(20, "3BHK")

    def test_bad_bhk_is_rejected(self):
        with self.assertRaises(ValueError):
            layout.parse_bhk("studio")


class BudgetTests(unittest.TestCase):
    def test_never_exceeds_budget(self):
        for area in (45, 70, 120, 250):
            for budget in (3_000, 15_000, 40_000, 200_000, 2_000_000):
                for bhk in ("1BHK", "2BHK", "3BHK"):
                    try:
                        rooms = layout.allocate(area, bhk)
                    except ValueError:
                        continue
                    items, _ = build_bom(rooms, budget, area)
                    self.assertLessEqual(sum(i.cost for i in items), budget, (area, budget, bhk))

    def test_mid_range_budgets_are_mostly_spent(self):
        # Up to roughly 450 per m2; beyond that the catalog runs out of items and money is left over.
        for budget in (5_000, 15_000, 40_000):
            rooms = layout.allocate(90, "2BHK")
            items, _ = build_bom(rooms, budget, 90)
            self.assertGreaterEqual(sum(i.cost for i in items), 0.8 * budget, budget)

    def test_bigger_budget_buys_a_higher_tier(self):
        rooms = layout.allocate(90, "2BHK")
        _, low = build_bom(rooms, 8_000, 90)
        _, high = build_bom(rooms, 300_000, 90)
        self.assertNotEqual(low, high)

    def test_tiny_budget_yields_fewer_items(self):
        rooms = layout.allocate(90, "2BHK")
        small, _ = build_bom(rooms, 500, 90)
        large, _ = build_bom(rooms, 80_000, 90)
        self.assertLess(len(small), len(large))

    def test_nonpositive_budget_rejected(self):
        with self.assertRaises(ValueError):
            build_bom(layout.allocate(90, "2BHK"), 0, 90)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = build_manifest(40_000, 90, "2BHK", "dark academia")
        self.nodes = self.manifest["topology"]["nodes"]

    def test_teleports_point_at_existing_rooms(self):
        ids = {n["room_id"] for n in self.nodes}
        for node in self.nodes:
            for spot in node["hotspots"]:
                if spot["type"] == "teleport":
                    self.assertIn(spot["payload"]["target_room_id"], ids)

    def test_every_room_is_reachable_from_the_first(self):
        graph = {n["room_id"]: [h["payload"]["target_room_id"] for h in n["hotspots"] if h["type"] == "teleport"]
                 for n in self.nodes}
        seen, stack = set(), [self.nodes[0]["room_id"]]
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack.extend(graph[current])
        self.assertEqual(seen, set(graph))

    def test_hotspot_yaws_do_not_collide(self):
        for node in self.nodes:
            yaws = [h["coordinates"]["yaw"] for h in node["hotspots"]]
            self.assertEqual(len(yaws), len(set(yaws)))

    def test_metadata_is_consistent(self):
        meta = self.manifest["metadata"]
        self.assertLessEqual(meta["spent"], meta["total_budget"])
        self.assertAlmostEqual(meta["spent"] + meta["remaining"], meta["total_budget"], places=2)

    def test_seeds_are_deterministic_and_distinct(self):
        self.assertEqual(seed_for(42, "room_a"), seed_for(42, "room_a"))
        self.assertNotEqual(seed_for(42, "room_a"), seed_for(42, "room_b"))
        again = build_manifest(40_000, 90, "2BHK", "dark academia")
        self.assertEqual(self.manifest, again)

    def test_prompts_carry_framing_and_style(self):
        for node in self.nodes:
            prompt = node["diffusion_parameters"]["positive_prompt"]
            self.assertIn("equirectangular", prompt)
            self.assertIn("dark academia", prompt)


class CurrencyTests(unittest.TestCase):
    def test_rupee_budget_is_actually_spent(self):
        # 30 lakh INR is a normal 2BHK budget; treating it as dollars used to spend about 2% of it.
        meta = build_manifest(3_000_000, 90, "2BHK", "x", currency="INR")["metadata"]
        self.assertLessEqual(meta["spent"], 3_000_000)
        self.assertGreater(meta["spent"], 0.8 * 3_000_000)

    def test_ceiling_holds_in_every_supported_currency(self):
        from engine.currency import PER_USD

        for code, per_usd in PER_USD.items():
            budget = 40_000 * per_usd
            meta = build_manifest(budget, 90, "2BHK", "x", currency=code)["metadata"]
            self.assertLessEqual(meta["spent"], budget, code)

    def test_item_costs_are_in_the_budget_currency(self):
        usd = build_manifest(40_000, 90, "2BHK", "x", currency="USD")["metadata"]["spent"]
        inr = build_manifest(40_000 * 85, 90, "2BHK", "x", currency="INR")["metadata"]["spent"]
        self.assertAlmostEqual(inr / 85, usd, delta=usd * 0.01)

    def test_unsupported_currency_is_rejected(self):
        with self.assertRaises(ValueError):
            build_manifest(40_000, 90, "2BHK", "x", currency="XYZ")


if __name__ == "__main__":
    unittest.main()
