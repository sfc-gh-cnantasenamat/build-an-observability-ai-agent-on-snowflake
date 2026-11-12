import json
import unittest
from pathlib import Path


DASHBOARD = Path(__file__).resolve().parents[1] / "dashboards" / "refund_truth.dash"


class DashboardTests(unittest.TestCase):
    def test_tile_fields_and_layouts_match_datasources(self):
        dashboard = json.loads(DASHBOARD.read_text())
        self.assertEqual([tab["tab_id"] for tab in dashboard["tabs"]],
                         ["overview", "investigate", "compare"])
        for tab in dashboard["tabs"]:
            positions = dashboard["layout"][tab["tab_id"]]["positions"]
            for tile in tab["tiles"]:
                self.assertIn(tile["tile_id"], positions)
                if tile["type"] == "text":
                    continue
                source_id, = tile["datasources"]
                datasource = dashboard["datasources"][source_id]
                fields = {column["name"] for column in datasource["columns"]}
                if "hierarchy_bindings" in datasource:
                    fields.update({"LEVEL_KEY", "LEVEL_LABEL"})
                if tile["type"] == "table":
                    used = {column["field"] for column in tile["spec"]["columns"]}
                else:
                    used = {item["field"] for item in tile["spec"]["encoding"].values()
                            if isinstance(item, dict) and "field" in item}
                self.assertFalse(used - fields, (tile["tile_id"], used - fields))

    def test_hierarchy_queries_have_member_and_row_placeholders(self):
        dashboard = json.loads(DASHBOARD.read_text())
        for datasource in dashboard["datasources"].values():
            for binding in datasource.get("hierarchy_bindings", []):
                name = binding["hierarchy_id"]
                self.assertEqual(datasource["sql"].count("{{ hierarchy_key('" + name + "') }}"), 2)
                self.assertIn("{{ hierarchy_rows('" + name + "') }}", datasource["sql"])
                self.assertIn(name, dashboard["hierarchies"])

    def test_chart_does_not_claim_unwired_drill(self):
        dashboard = json.loads(DASHBOARD.read_text())
        chart = next(tile for tile in dashboard["tabs"][0]["tiles"]
                     if tile["tile_id"] == "run-hierarchy")
        if not chart.get("interactions"):
            self.assertNotIn("Drill", chart["title"])

    def test_flat_evidence_tile_is_ordered_and_does_not_claim_expansion(self):
        dashboard = json.loads(DASHBOARD.read_text())
        tile = next(tile for tile in dashboard["tabs"][1]["tiles"]
                    if tile["tile_id"] == "evidence-hierarchy")
        self.assertNotIn("Expand", tile["title"])
        source = dashboard["datasources"][tile["datasources"][0]]
        self.assertIn("EVENT_ORDINAL", source["sql"])
        self.assertNotIn("hierarchy_bindings", source)



if __name__ == "__main__":
    unittest.main()