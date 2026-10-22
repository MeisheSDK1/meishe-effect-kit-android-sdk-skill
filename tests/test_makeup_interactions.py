from __future__ import annotations

import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from verify_makeup_interactions import parse_nodes, require_node  # noqa: E402


class MakeupInteractionVerifierTests(unittest.TestCase):
    def test_parses_accessibility_id_selected_state_and_center(self) -> None:
        nodes = parse_nodes(
            """
            <hierarchy>
              <node content-desc="nve-makeup-item-barbie"
                    selected="true" bounds="[10,20][50,80]" />
            </hierarchy>
            """
        )
        node = require_node(nodes, "nve-makeup-item-barbie")
        self.assertTrue(node.selected)
        self.assertEqual((30, 50), node.center)

    def test_missing_node_lists_visible_accessibility_ids(self) -> None:
        nodes = parse_nodes(
            """
            <hierarchy>
              <node content-desc="nve-makeup-entry"
                    selected="false" bounds="[0,0][20,20]" />
            </hierarchy>
            """
        )
        with self.assertRaisesRegex(
            AssertionError, "nve-makeup-entry"
        ):
            require_node(nodes, "nve-makeup-tab-compose")


if __name__ == "__main__":
    unittest.main()
