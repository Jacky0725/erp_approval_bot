from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reagent_data_minimizer import is_ignored_reagent_metadata_key, minimize_reagent_record
from reagent_identity import ReagentIdentityResolver


def test_packaging_metadata_is_removed_but_identity_and_approval_fields_remain():
    source = {
        "序号": "7", "试剂名称": "乙醇", "CAS号": "64-17-5", "物化特性": "-",
        "规格": "500 mL", "规格单位": "瓶", "包装方式": "箱装", "box_num": "12", "spec_unit_id": "瓶",
    }
    result = minimize_reagent_record(source)
    assert result == {"序号": "7", "试剂名称": "乙醇", "CAS号": "64-17-5", "物化特性": "-"}
    assert is_ignored_reagent_metadata_key("包装方式")
    assert is_ignored_reagent_metadata_key("spec_unit_id")
    assert not is_ignored_reagent_metadata_key("CAS号")


def test_identity_resolution_does_not_treat_specification_as_mixture_evidence(tmp_path):
    resolver = ReagentIdentityResolver(settings={}, root_dir=tmp_path)
    result = resolver.resolve("氯化钠", specification="混合液 500 mL", unit="瓶")
    assert result.mixture_state == "single_substance"
