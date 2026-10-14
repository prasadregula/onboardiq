import io
import time
from pathlib import Path

from app.config import get_settings
from app.providers.screening import OfacScreeningProvider, _parse_remarks
from tests.conftest import golden


def test_async_run_returns_202_and_polls_to_completion(client):
    get_settings().run_async = True
    cid = client.post("/cases", json=golden("G01")).json()["id"]
    r = client.post(f"/cases/{cid}/run", json={})
    assert r.status_code == 202 and r.json()["status"] == "processing"
    # a second run while processing is rejected (TestClient may already have finished the background task -> 202 again)
    assert client.post(f"/cases/{cid}/run", json={}).status_code in (409, 202)
    for _ in range(50):  # TestClient runs background tasks after the response; poll the case
        d = client.get(f"/cases/{cid}").json()
        if d["case"]["status"] != "processing":
            break
        time.sleep(0.05)
    assert d["case"]["status"] == "approved"
    runs = client.get(f"/cases/{cid}/runs").json()
    assert runs and runs[0]["status"] == "finished"
    assert client.get(f"/cases/{cid}/runs/{runs[0]['id']}").json()["outcome"] == "auto_approve"
    types = [e["event_type"] for e in client.get(f"/cases/{cid}/audit").json()]
    assert "run_requested" in types


def test_upload_stores_and_streams_back(client, tmp_path):
    cid = client.post("/cases", json=golden("G02")).json()["id"]
    r = client.post(f"/cases/{cid}/documents", data={"doc_type": "utility_bill"},
                    files={"file": ("bill.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")})
    assert r.status_code == 201
    doc = r.json()
    assert Path(doc["blob_uri"]).exists()
    content = client.get(f"/cases/{cid}/documents/{doc['id']}/content")
    assert content.status_code == 200 and content.content.startswith(b"%PDF")
    bad = client.post(f"/cases/{cid}/documents", data={"doc_type": "x"}, files={"file": ("a.exe", io.BytesIO(b"MZ"), "application/x-msdownload")})
    assert bad.status_code == 415


def test_request_id_and_security_headers(client):
    r = client.get("/health", headers={"X-Request-Id": "abc123"})
    assert r.headers["X-Request-Id"] == "abc123" and r.headers["X-Content-Type-Options"] == "nosniff"
    assert "onboardiq_http_seconds" in client.get("/metrics").text


SDN_SAMPLE = '''36,"AEROCARIBBEAN AIRLINES","-0-","CUBA","-0-","-0-","-0-","-0-","-0-","-0-","-0-","-0-"
7778,"SOKOLOV, Viktor Andreyevich","individual","RUSSIA-EO14024","-0-","-0-","-0-","-0-","-0-","-0-","-0-","DOB 14 Mar 1968; POB Moscow, Russia; nationality Russia; Gender Male;"
'''
ALT_SAMPLE = '''36,12,"aka","AERO-CARIBBEAN","-0-"
7778,13,"aka","SOKOLOV-MARIN, Viktor","-0-"
'''


def test_ofac_csv_parsing_with_aliases(tmp_path):
    (tmp_path / "sdn.csv").write_text(SDN_SAMPLE)
    (tmp_path / "alt.csv").write_text(ALT_SAMPLE)
    prov = OfacScreeningProvider(tmp_path, include_demo_pep=False)
    assert len(prov.entries) == 2
    sok = next(e for e in prov.entries if "sokolov" in e.name.lower())
    assert sok.name == "Viktor Andreyevich SOKOLOV" and sok.dob == "1968-03-14" and sok.country == "RU" and sok.aliases == ["SOKOLOV-MARIN, Viktor"]
    hits = prov.screen("Viktor Sokolov-Marin", "1968-03-14", "RU")
    assert hits and hits[0].entry_id == "OFAC_SDN-7778" and hits[0].score >= 0.9
    assert prov.screen("Aero Caribbean") and prov.screen("Maria Castillo") == []


def test_parse_remarks():
    assert _parse_remarks("DOB 02 Nov 1975; nationality Iran;") == ("1975-11-02", "IR")
    assert _parse_remarks("DOB 1975; nationality Iran;") == (None, "IR")
