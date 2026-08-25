"""DFM constants, the cached rules block, error hints and content policy."""

from __future__ import annotations

import pytest

from formforge import dfm, hints, policy


class TestLimits:
    def test_thresholds_derive_from_the_nozzle(self):
        fine = dfm.limits_for("generic_fdm_0.4")
        coarse = dfm.limits_for("generic_fdm_0.6")
        assert coarse.min_wall_fail_mm > fine.min_wall_fail_mm
        assert fine.min_wall_fail_mm == pytest.approx(0.8)
        assert fine.min_wall_warn_mm == pytest.approx(1.2)

    def test_flexible_material_demands_thicker_walls(self):
        pla = dfm.limits_for("generic_fdm_0.4", "PLA")
        tpu = dfm.limits_for("generic_fdm_0.4", "TPU")
        assert tpu.min_wall_fail_mm > pla.min_wall_fail_mm
        assert tpu.max_bridge_warn_mm < pla.max_bridge_warn_mm

    def test_unknown_profile_names_the_alternatives(self):
        with pytest.raises(KeyError) as excinfo:
            dfm.get_profile("no_such_printer")
        assert "generic_fdm_0.4" in str(excinfo.value)

    def test_unknown_material_falls_back_to_pla(self):
        assert dfm.get_material("unobtanium").id == "PLA"


class TestRulesBlock:
    def test_is_byte_stable(self):
        """The rules block is a cached prompt prefix.

        Any variation between calls -- a timestamp, dict ordering, a float that
        formats differently -- invalidates the cache on every request and
        multiplies the input cost of the whole codegen path.
        """
        first = dfm.rules_block("bambu_p1s_0.4", "PETG")
        second = dfm.rules_block("bambu_p1s_0.4", "PETG")
        assert first == second

    def test_reflects_the_profile_it_was_asked_for(self):
        block = dfm.rules_block("generic_fdm_0.6")
        assert "0.6 mm nozzle" in block
        assert "1.8 mm (3 perimeters)" in block

    def test_agrees_with_the_validator_thresholds(self):
        """The prompt and the validator must not disagree.

        A model told 1.2 mm is fine and then failed at 1.2 mm would loop until
        it exhausted its budget, having done nothing wrong.
        """
        limits = dfm.limits_for("generic_fdm_0.4", "PLA")
        block = dfm.rules_block("generic_fdm_0.4", "PLA")
        assert f"{limits.min_wall_warn_mm:g} mm (3 perimeters)" in block
        assert f"Minimum standalone feature: {limits.min_feature_mm:g} mm" in block

    def test_category_rules_are_appended(self):
        block = dfm.category_rules_block("planter")
        assert "drainage" in block.lower()
        assert dfm.category_rules_block(None) == ""


class TestHints:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("BRepFilletAPI_MakeFillet failed", "FilletFailure"),
            ("StdFail_NotDone", "KernelNotDone"),
            ("BOPAlgo_Builder error in boolean", "BooleanFailure"),
            ("BRepOffset_MakeOffset: shell failed", "OffsetFailure"),
            ("ValueError: wire is not closed", "OpenWireFailure"),
            ("MemoryError: cannot allocate", "OutOfMemory"),
            ("RecursionError: maximum recursion depth", "RecursionLimit"),
        ],
    )
    def test_maps_kernel_errors_to_named_causes(self, text, expected):
        error_class, hint = hints.classify(text)
        assert error_class == expected
        assert len(hint) > 40, "a hint that does not say what to change is useless"

    def test_unknown_errors_get_a_generic_but_actionable_hint(self):
        error_class, hint = hints.classify("something nobody has seen before")
        assert error_class == "UnknownError"
        assert hint == hints.GENERIC_HINT

    def test_the_fillet_hint_names_the_actual_cause(self):
        _, hint = hints.classify("BRepFilletAPI failure")
        assert "radius" in hint.lower()


class TestPolicy:
    @pytest.mark.parametrize(
        "prompt",
        [
            "a Pikachu keychain",
            "mickey mouse wall hook",
            "a keychain with the Nike swoosh on it",
            "baby yoda planter",
        ],
    )
    def test_refuses_protected_characters_and_brands(self, prompt):
        result = policy.classify(prompt)
        assert result.decision is policy.Decision.REFUSE
        assert result.category == "ip"
        assert "protected" in result.user_message() or "trademark" in result.user_message()

    @pytest.mark.parametrize(
        "prompt",
        ["an AR-15 lower receiver", "a suppressor baffle", "brass knuckles", "a glock frame"],
    )
    def test_refuses_weapon_components(self, prompt):
        result = policy.classify(prompt)
        assert result.decision is policy.Decision.REFUSE
        assert result.category == "weapon"

    @pytest.mark.parametrize(
        "prompt",
        [
            "a mouse pad holder for my desk",
            "a star-shaped wall tile",
            "a shield-shaped luggage tag",
            "a hex planter for a 4 inch pot",
            "a cable clip for a 6mm cable",
            "a drawer divider for kitchen utensils",
        ],
    )
    def test_allows_ordinary_requests_that_share_a_word(self, prompt):
        """A word that is also an ordinary maker term must not trigger a refusal.

        Refusing "mouse pad holder" over "mouse" would be both useless and
        infuriating, and false positives here cost real users real generations.
        """
        assert policy.classify(prompt).decision is policy.Decision.ALLOW

    def test_flags_ambiguous_terms_without_blocking(self):
        result = policy.classify("a knife block for the kitchen counter")
        assert result.decision is policy.Decision.FLAG
        assert result.allowed

    def test_screens_the_text_destined_for_the_model_surface(self):
        """'Put NIKE on it' is the same problem as 'make a Nike keychain'."""
        result = policy.classify("a rectangular name tag", text_content="NIKE")
        assert result.decision is policy.Decision.REFUSE
