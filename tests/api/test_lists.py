"""Source trust list CRUD."""

from __future__ import annotations


def test_lists_start_empty(client):
    response = client.get("/v1/lists")
    assert response.status_code == 200
    assert response.json() == []


def test_add_list_and_read_it_back(client):
    payload = {
        "list_type": "black",
        "match_type": "domain",
        "pattern": "law-blog.example",
        "reason": "user-generated summaries, no editorial review",
    }
    created = client.post("/v1/lists", json=payload)
    assert created.status_code == 201

    body = created.json()
    assert body["id"]
    assert body["list_type"] == "black"
    assert body["active"] is True

    listed = client.get("/v1/lists").json()
    assert [e["pattern"] for e in listed] == ["law-blog.example"]


def test_delete_removes_the_entry(client):
    entry_id = client.post(
        "/v1/lists", json={"list_type": "gray", "pattern": "aggregator.example"}
    ).json()["id"]

    assert client.delete(f"/v1/lists/{entry_id}").status_code == 204
    assert client.get("/v1/lists").json() == []


def test_delete_unknown_entry_is_404(client):
    assert client.delete("/v1/lists/does-not-exist").status_code == 404


def test_match_explains_which_rule_fired(client):
    """The panel says 'blacklisted'; a user's next question is 'by what rule?'."""
    client.post(
        "/v1/lists",
        json={
            "list_type": "black",
            "pattern": "law-blog.example",
            "reason": "no editorial review",
        },
    )

    matched = client.get("/v1/lists/match", params={"domain": "posts.law-blog.example"}).json()
    assert matched["list_type"] == "black"
    assert matched["reason"] == "no editorial review"

    assert client.get("/v1/lists/match", params={"domain": "elitigation.sg"}).json() is None


def test_invalid_list_type_is_rejected(client):
    response = client.post("/v1/lists", json={"list_type": "purple", "pattern": "x.example"})
    assert response.status_code == 422
