from __future__ import annotations

import os
from datetime import UTC, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from albion_crafter.core.crafting_profile import (
    CraftingSkillLevel,
    CraftingSkillProfile,
    ManualFocusEfficiencyOverride,
)
from albion_crafter.core.models import Item, MaterialRequirement, Recipe
from albion_crafter.core.provenance import Provenance
from albion_crafter.core.stations import StationType
from albion_crafter.database.database import Database
from albion_crafter.database.v3 import StationFeeRepository
from albion_crafter.market.models import Region
from albion_crafter.ui.settings_view import SettingsView


class MemorySettings:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    def set_many(self, values) -> None:
        self.values.update(values)


class MemoryProfiles:
    def __init__(self, profile: CraftingSkillProfile) -> None:
        self.profile = profile

    def load(self, profile_id: str = "default") -> CraftingSkillProfile:
        return self.profile

    def save(
        self,
        profile: CraftingSkillProfile,
        profile_id: str = "default",
        *,
        name: str = "Default",
    ) -> None:
        self.profile = profile


class OneRecipeCatalog:
    def __init__(self, recipe: Recipe) -> None:
        self.recipe = recipe

    def search_recipes(self, query: str = "", *, limit: int = 100):
        return [self.recipe.output]

    def get_recipe(self, item_id: str) -> Recipe | None:
        return self.recipe if item_id == self.recipe.output.item_id else None


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _recipe(*, returnable: bool) -> Recipe:
    return Recipe(
        output=Item(
            "T4_MAIN_SWORD_ARTIFACT",
            "Artifact Sword",
            4,
            crafting_category="sword",
            max_quality=5,
        ),
        output_quantity=1,
        materials=(MaterialRequirement("T4_COMPONENT", 1, returnable),),
        item_value=100,
        base_focus_cost=200,
        provenance=Provenance.STATIC_GAME_DATA,
    )


def test_profile_editor_previews_unsaved_levels_and_manual_override(qt_app) -> None:
    profile = CraftingSkillProfile(assume_zero_for_unspecified=True)
    profiles = MemoryProfiles(profile)
    view = SettingsView(
        MemorySettings(),
        crafting_profiles=profiles,  # type: ignore[arg-type]
        catalog=OneRecipeCatalog(_recipe(returnable=True)),  # type: ignore[arg-type]
    )

    view.mastery_level.setValue(50)
    view.specialization_level.setValue(50)
    assert "effective FCE 15,500" in view.profile_resolution.text()

    view.manual_fce_enabled.setChecked(True)
    view.manual_fce.setValue(12_345)
    assert "effective FCE 12,345" in view.profile_resolution.text()
    assert "manual_override" in view.profile_resolution.text()
    view.close()


def test_manual_override_for_unverified_recipe_preserves_underlying_skills(qt_app) -> None:
    original_skills = (
        CraftingSkillLevel("sword:mastery", "sword", 70, 15),
        CraftingSkillLevel("sword:main_sword_artifact", "sword", 80, 15),
    )
    profiles = MemoryProfiles(
        CraftingSkillProfile(
            available_focus=10_000,
            skill_levels=original_skills,
            complete_groups=frozenset({"sword"}),
        )
    )
    view = SettingsView(
        MemorySettings(),
        crafting_profiles=profiles,  # type: ignore[arg-type]
        catalog=OneRecipeCatalog(_recipe(returnable=False)),  # type: ignore[arg-type]
    )
    assert not view.mastery_level.isEnabled()
    assert not view.specialization_level.isEnabled()
    defaults_saved = []
    profile_changed = []
    view.settings_saved.connect(lambda: defaults_saved.append(True))
    view.crafting_profile_changed.connect(lambda: profile_changed.append(True))

    view.manual_fce_enabled.setChecked(True)
    view.manual_fce.setValue(12_345)
    view._save_profile_item()

    assert profiles.profile.skill_levels == original_skills
    assert profiles.profile.manual_fce_overrides[0].focus_cost_efficiency == 12_345
    assert profile_changed == [True]
    assert defaults_saved == []
    view.close()


def test_verified_level_edit_preserves_persisted_node_coefficients(qt_app) -> None:
    profiles = MemoryProfiles(
        CraftingSkillProfile(
            available_focus=10_000,
            skill_levels=(
                CraftingSkillLevel("sword:mastery", "sword", 70, 15),
                CraftingSkillLevel("sword:main_sword_artifact", "sword", 80, 15),
            ),
            complete_groups=frozenset({"sword"}),
        )
    )
    view = SettingsView(
        MemorySettings(),
        crafting_profiles=profiles,  # type: ignore[arg-type]
        catalog=OneRecipeCatalog(_recipe(returnable=True)),  # type: ignore[arg-type]
    )
    view.mastery_level.setValue(71)
    view.specialization_level.setValue(81)
    view._save_profile_item()

    stored = {level.skill_key: level for level in profiles.profile.skill_levels}
    assert stored["sword:mastery"].level == 71
    assert stored["sword:mastery"].mutual_fce_per_level == 15
    assert stored["sword:main_sword_artifact"].level == 81
    assert stored["sword:main_sword_artifact"].mutual_fce_per_level == 15
    view.close()


def test_settings_edits_refining_tier_profile_and_refining_station_fee(
    qt_app,
    tmp_path,
) -> None:
    recipe = Recipe(
        Item("T6_METALBAR@2", "Runite Steel Bar", 6, 2, crafting_category="ore"),
        1,
        (
            MaterialRequirement("T6_ORE@2", 4, True),
            MaterialRequirement("T5_METALBAR@2", 1, True),
        ),
        item_value=20,
        base_focus_cost=164,
        provenance=Provenance.STATIC_GAME_DATA,
    )
    profiles = MemoryProfiles(CraftingSkillProfile())
    database = Database(tmp_path / "settings-refining.db")
    database.initialize()
    fees = StationFeeRepository(database)
    view = SettingsView(
        MemorySettings(),
        station_fees=fees,
        crafting_profiles=profiles,  # type: ignore[arg-type]
        catalog=OneRecipeCatalog(recipe),  # type: ignore[arg-type]
    )

    refining_stations = {
        StationType.SMELTER,
        StationType.LUMBERMILL,
        StationType.TANNER,
        StationType.WEAVER,
        StationType.STONEMASON,
    }
    assert refining_stations <= {
        view.station_type.itemData(index) for index in range(view.station_type.count())
    }
    assert "refining:ore:t6" in view.profile_mapping.text()
    assert not view.mastery_level.isEnabled()
    view.specialization_level.setValue(42)
    view.complete_profile_group.setChecked(True)
    view._save_profile_item()
    stored = {level.skill_key: level for level in profiles.profile.skill_levels}
    assert set(stored) == {"refining:ore:t6"}
    assert stored["refining:ore:t6"].level == 42
    assert stored["refining:ore:t6"].mutual_fce_per_level == 30
    assert profiles.profile.complete_groups == frozenset({"refining:ore"})

    view.station_city.setCurrentText("Thetford")
    view.station_type.setCurrentIndex(view.station_type.findData(StationType.SMELTER))
    view.station_displayed_fee.setValue(750)
    view._save_station_fee()
    saved = fees.get(Region.AMERICAS, "Thetford", StationType.SMELTER)
    assert saved is not None and saved.displayed_fee == 750
    view.close()


def test_refining_matrix_distinguishes_blank_zero_completeness_and_manual_override(
    qt_app,
) -> None:
    manual = ManualFocusEfficiencyOverride(
        "refine/ore/t6",
        12_345,
        datetime(2026, 8, 19, tzinfo=UTC),
    )
    crafting_level = CraftingSkillLevel("sword:mastery", "sword", 50, 15)
    profiles = MemoryProfiles(
        CraftingSkillProfile(
            skill_levels=(crafting_level,),
            manual_fce_overrides=(manual,),
        )
    )
    view = SettingsView(
        MemorySettings(),
        crafting_profiles=profiles,  # type: ignore[arg-type]
    )

    assert view.refining_matrix_inputs[("ore", 4)].text() == ""
    assert "Unknown" in view.refining_matrix_resolution["ore"].text()
    view.refining_matrix_inputs[("ore", 4)].setText("0")
    view.refining_matrix_inputs[("ore", 6)].setText("42")
    assert "Unknown" in view.refining_matrix_resolution["ore"].text()
    view.refining_matrix_complete["ore"].setChecked(True)
    assert "T4 1,260" in view.refining_matrix_resolution["ore"].text()
    assert "T6 12,345 (manual_override)" in view.refining_matrix_resolution["ore"].text()

    view._save_refining_matrix()
    stored = {level.skill_key: level for level in profiles.profile.skill_levels}
    assert stored["sword:mastery"] == crafting_level
    assert stored["refining:ore:t4"].level == 0
    assert stored["refining:ore:t6"].level == 42
    assert "refining:wood:t4" not in stored
    assert "refining:ore" in profiles.profile.complete_groups
    assert profiles.profile.manual_fce_overrides == (manual,)
    view.close()
