"""Tests for postcode parsing, the transport layer, the risk engine and the API."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import policy
from app.clients import ClientBook
from app.config import Settings
from app.postcode_client import (
    PostcodeClient,
    PostcodeError,
    SimulatedTransport,
)
from app.postcodes import depth, is_valid, normalise, parse
from app.risk_engine import RiskEngine


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


class TestParsing:
    def test_all_styles_normalise_the_same(self):
        expected = "EK-01-A03-FK-01"
        for style in ["EK-01-A03-FK-01", "EK 01 A03 FK 01", "ek01a03fk01", "EK-01-A03-FK-01 "]:
            assert normalise(style) == expected

    def test_segments_are_split_correctly(self):
        seg = parse("LA-02-D22-ZR-63")
        assert seg.as_dict() == {
            "state": "LA",
            "lga": "02",
            "district": "D22",
            "area": "ZR",
            "unit": "63",
        }

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "EK-01-A03-FK",          # too few segments
            "EK-01-A03-FK-01-99",    # too many
            "E-01-A03-FK-01",        # state must be 2 alpha
            "EK-1-A03-FK-01",        # lga must be 2 digits
            "EK-01-A03-11-01",       # area must be 2 alpha
            "EK-01-A03-FK-AB",       # unit must be numeric
            "nonsense",
        ],
    )
    def test_invalid_codes_are_rejected(self, bad):
        assert not is_valid(bad)
        with pytest.raises(ValueError):
            parse(bad)

    def test_depth_counts_filled_segments(self):
        assert depth("") == 0
        assert depth("EK") == 1
        assert depth("EK01") == 2
        assert depth("EK01A03") == 3
        assert depth("EK01A03FK") == 4
        assert depth("EK01A03FK01") == 5


# --------------------------------------------------------------------------- #
# Transport behaviour
# --------------------------------------------------------------------------- #


class TestSimulatedTransport:
    def setup_method(self):
        self.t = SimulatedTransport(max_level=3)

    def test_levels_are_cumulative(self):
        l1 = self.t.lookup("FC-04-A01-MH-01", 1)
        assert l1.valid and l1.state_name is None and l1.building_use_status is None

        l2 = self.t.lookup("FC-04-A01-MH-01", 2)
        assert l2.state_name == "FEDERAL CAPITAL TERRITORY"
        assert l2.building_use_status is None

        l3 = self.t.lookup("FC-04-A01-MH-01", 3)
        assert l3.building_use_status == "commercial"
        assert l3.other_building_info is None

    def test_l1_is_free_and_l2_plus_meters(self):
        before = self.t.credits_spent
        self.t.lookup("FC-04-A01-MH-01", 1)
        assert self.t.credits_spent == before
        self.t.lookup("FC-04-A01-MH-01", 3)
        assert self.t.credits_spent == before + 2

    def test_level_beyond_grant_is_refused(self):
        with pytest.raises(PostcodeError) as exc:
            self.t.lookup("FC-04-A01-MH-01", 5)
        assert exc.value.code == "level_not_granted"
        assert exc.value.status == 403

    def test_valid_structure_unknown_building_is_not_valid(self):
        result = self.t.lookup("FC-99-Z99-ZZ-99", 1)
        assert result.valid is False

    def test_reverse_respects_radius_and_ceiling(self):
        hit = self.t.reverse(7.4746, 9.0765, 25)
        assert hit["found"] and hit["unit"]["confidence"] == "high"

        miss = self.t.reverse(0.0, 0.0, 25)
        assert miss["found"] is False

        # A caller asking for more than the 250 m ceiling is silently clamped.
        clamped = self.t.reverse(7.4746, 9.0765, 10_000)
        assert clamped["radius_m"] == 250

    def test_autocomplete_reports_active_segment(self):
        ac = self.t.autocomplete("LA02")
        assert ac["segment"] in {"state", "lga", "district", "area", "unit"}
        assert any(s["code"].startswith("LA-02") for s in ac["suggestions"])


class TestGradedLookup:
    def test_degrades_when_level_not_granted(self):
        # A key granted only L2 must still get a result when the caller asks L4.
        client = PostcodeClient(SimulatedTransport(max_level=2), max_level=2)
        result = client.lookup_graded("FC-04-A01-MH-01", 4)
        assert result.valid and result.level_served == 2
        assert result.building_use_status is None


# --------------------------------------------------------------------------- #
# Risk engine
# --------------------------------------------------------------------------- #


def _engine(max_level: int = 3, review: int = 40, block: int = 70) -> RiskEngine:
    cfg = Settings(
        postcode_api_key="",
        postcode_api_base="https://api.postcode.gov.irrelevant",
        max_level=max_level,
        risk_review_threshold=review,
        risk_block_threshold=block,
    )
    return RiskEngine(PostcodeClient(SimulatedTransport(max_level=max_level), max_level), cfg)


class TestRiskEngine:
    def test_clean_client_scores_low(self):
        a = _engine().assess(
            {
                "id": "X",
                "name": "Clean Software Ltd",
                "sector": "software",
                "declared_address": "24 Oba Akran Avenue, Ikeja GRA, Lagos",
                "postcode": "LA-01-C11-TC-10",
                "declared_coordinates": [3.3417, 6.5833],
                "registration_status": "active",
                "rc_number": "RC1",
                "tax_id": "TIN1",
                "business_description": "Software development.",
                "directors": [{"name": "A", "id_verified": True}],
            }
        )
        assert a.decision == "approve"
        assert a.score == 0

    def test_address_mismatch_is_flagged(self):
        a = _engine().assess(
            {
                "id": "X",
                "name": "Wrong Address Ltd",
                "sector": "software",
                "declared_address": "1 Completely Different Street, Somewhere Else",
                "postcode": "LA-01-C11-TC-10",
                "registration_status": "active",
                "rc_number": "RC1",
                "tax_id": "TIN1",
                "directors": [{"name": "A", "id_verified": True}],
            }
        )
        ids = {s.id for s in a.signals}
        assert "address_mismatch" in ids or "area_mismatch" in ids

    def test_geo_mismatch_is_flagged(self):
        a = _engine().assess(
            {
                "id": "X",
                "name": "Far Away Ltd",
                "sector": "software",
                "declared_address": "24 Oba Akran Avenue, Ikeja GRA, Lagos",
                "postcode": "LA-01-C11-TC-10",
                "declared_coordinates": [9.8442, 10.3103],  # Bauchi, not Lagos
                "registration_status": "active",
                "rc_number": "RC1",
                "tax_id": "TIN1",
                "directors": [{"name": "A", "id_verified": True}],
            }
        )
        assert "geo_mismatch" in {s.id for s in a.signals}

    def test_sanctions_terms_block(self):
        a = _engine().assess(
            {
                "id": "X",
                "name": "Sanctioned Holdings Ltd",
                "sector": "software",
                "postcode": "LA-01-C11-TC-10",
                "declared_address": "24 Oba Akran Avenue, Ikeja GRA, Lagos",
                "registration_status": "active",
                "rc_number": "RC1",
                "tax_id": "TIN1",
                "directors": [{"name": "A", "id_verified": True}],
            }
        )
        assert a.decision == "block"
        assert any("Sanctions" in s.label for s in a.signals)

    def test_pep_and_sector_raise_score(self):
        a = _engine().assess(
            {
                "id": "X",
                "name": "Payments Ltd",
                "sector": "money_services",
                "postcode": "LA-04-F44-BL-69",
                "declared_address": "58 Herbert Macaulay Road, Yaba, Lagos",
                "registration_status": "active",
                "rc_number": "RC1",
                "tax_id": "TIN1",
                "directors": [{"name": "A", "id_verified": True, "pep": True}],
            }
        )
        ids = {s.id for s in a.signals}
        assert "pep_exposure" in ids and "sector_inherent_risk" in ids

    def test_shared_unit_cluster_is_flagged(self):
        a = _engine().assess(
            {
                "id": "X",
                "name": "Cluster Ltd",
                "sector": "financial_services",
                "postcode": "FC-04-R76-DA-10",
                "declared_address": "Plot 1100, Shehu Shagari Way, Asokoro, Abuja",
                "registration_status": "active",
                "rc_number": "RC1",
                "tax_id": "TIN1",
                "directors": [{"name": "A", "id_verified": True}],
            },
            peer_count=3,
        )
        assert "shared_unit" in {s.id for s in a.signals}

    def test_score_is_deterministic(self):
        payload = {
            "id": "X",
            "name": "Repeat Ltd",
            "sector": "money_services",
            "postcode": "LA-04-F44-BL-69",
            "declared_address": "58 Herbert Macaulay Road, Yaba, Lagos",
            "registration_status": "active",
            "rc_number": "RC1",
            "tax_id": "TIN1",
            "directors": [{"name": "A", "id_verified": True, "pep": True}],
        }
        eng = _engine()
        assert eng.assess(payload).score == eng.assess(payload).score

    def test_every_signal_has_a_mapped_action(self):
        """Guard against a signal being added without an operator instruction."""
        a = _engine().assess(
            {
                "id": "X",
                "name": "Everything Wrong Ltd",
                "sector": "gambling",
                "postcode": None,
                "registration_status": "inactive",
                "rc_number": None,
                "tax_id": None,
                "business_description": "ponzi shell company",
                "directors": [{"name": "A", "id_verified": False}],
            }
        )
        assert a.required_actions
        assert not any(s.weight == 0 for s in a.signals)


# --------------------------------------------------------------------------- #
# Client book
# --------------------------------------------------------------------------- #


class TestClientBook:
    def test_peer_counting(self):
        book = ClientBook()
        # The three Asokoro entities share a postcode.
        assert book.peers_at("FC-04-R76-DA-10") == 3
        assert book.peers_at("LA-01-C11-TC-10") == 1
        assert book.peers_at(None) == 1

    def test_all_seeded_postcodes_are_well_formed(self):
        for client in ClientBook().all():
            pc = client.get("postcode")
            if pc:
                assert is_valid(pc), f"{client['id']} has malformed postcode {pc}"


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    # Rebuild modules that captured settings at import time.
    import importlib

    import app.config
    import app.db
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.db)
    importlib.reload(app.main)
    return TestClient(app.main.app)


class TestApi:
    def test_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["mode"] == "simulated"

    def test_verify_known_client(self, client):
        r = client.get("/api/verify/CLI-0011")
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == "block"
        assert body["record_id"] >= 1

    def test_verify_unknown_client_404(self, client):
        assert client.get("/api/verify/NOPE").status_code == 404

    def test_verify_ad_hoc(self, client):
        r = client.post(
            "/api/verify",
            json={
                "id": "ADHOC",
                "name": "Ad hoc Ltd",
                "sector": "software",
                "postcode": "LA-01-C11-TC-10",
                "declared_address": "24 Oba Akran Avenue, Ikeja GRA, Lagos",
                "rc_number": "RC1",
                "tax_id": "TIN1",
                "directors": [{"name": "A", "id_verified": True}],
            },
        )
        assert r.status_code == 200
        assert r.json()["decision"] == "approve"

    def test_portfolio_screen_counts_add_up(self, client):
        body = client.post("/api/portfolio/screen").json()
        total = sum(body["decisions"].values())
        assert total == body["count"] == len(body["results"])
        scores = [r["score"] for r in body["results"]]
        assert scores == sorted(scores, reverse=True)

    def test_postcode_lookup_and_level_gate(self, client):
        ok = client.get("/api/postcode/lookup", params={"code": "FC-04-A01-MH-01", "level": 3})
        assert ok.status_code == 200
        assert ok.json()["building_use_status"] == "commercial"
        # max_level is 3, so L5 is refused by the transport.
        denied = client.get("/api/postcode/lookup", params={"code": "FC-04-A01-MH-01", "level": 5})
        assert denied.status_code == 403

    def test_postcode_disassemble(self, client):
        body = client.get("/api/postcode/disassemble", params={"code": "ek01a03fk01"}).json()
        assert body["compact"] == "EK01A03FK01"
        assert body["data"]["state"] == "EK"

    def test_postcode_disassemble_rejects_garbage(self, client):
        assert client.get("/api/postcode/disassemble", params={"code": "garbage"}).status_code == 400

    def test_audit_records_verifications(self, client):
        client.get("/api/verify/CLI-0001")
        entries = client.get("/api/audit").json()["entries"]
        assert any(e["action"] == "verify_client" for e in entries)

    def test_audit_hashes_personal_identifiers(self, client):
        # The store's hash helper must not return the raw value.
        from app.db import hash_identifier

        digest = hash_identifier("ada@example.com")
        assert digest != "ada@example.com"
        assert len(digest) == 64
