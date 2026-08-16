import pytest
from uuid import uuid4

from app.api.routes import admin_docs, docs as docs_route


@pytest.mark.asyncio
async def test_admin_docs_crud_and_user_visibility(async_client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}

    create_resp = await async_client.post(
        "/api/v1/admin/docs",
        headers=headers,
        json={
            "title": "Getting started",
            "content": "# Welcome\n\nThis is a document.",
            "is_visible": True,
            "sort_order": 2,
        },
    )
    assert create_resp.status_code == 201
    created = create_resp.json()["data"]
    doc_id = created["id"]

    list_resp = await async_client.get("/api/v1/admin/docs", headers=headers)
    assert list_resp.status_code == 200
    assert len(list_resp.json()["data"]["items"]) == 1

    user_resp = await async_client.get("/api/v1/docs", headers=headers)
    assert user_resp.status_code == 200
    assert len(user_resp.json()["data"]["items"]) == 1

    update_resp = await async_client.patch(
        f"/api/v1/admin/docs/{doc_id}",
        headers=headers,
        json={"is_visible": False, "title": "Hidden document"},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["data"]["is_visible"] is False

    hidden_resp = await async_client.get("/api/v1/docs", headers=headers)
    assert hidden_resp.status_code == 200
    assert hidden_resp.json()["data"]["items"] == []

    delete_resp = await async_client.delete(f"/api/v1/admin/docs/{doc_id}", headers=headers)
    assert delete_resp.status_code == 200
    assert delete_resp.json()["success"] is True


@pytest.mark.asyncio
async def test_admin_docs_filter(async_client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}

    await async_client.post(
        "/api/v1/admin/docs",
        headers=headers,
        json={
            "title": "Visible document",
            "content": "visible content",
            "is_visible": True,
            "sort_order": 1,
        },
    )
    await async_client.post(
        "/api/v1/admin/docs",
        headers=headers,
        json={
            "title": "Hidden document",
            "content": "hidden content",
            "is_visible": False,
            "sort_order": 3,
        },
    )

    visible_resp = await async_client.get("/api/v1/admin/docs?visible=true", headers=headers)
    assert visible_resp.status_code == 200
    visible_items = visible_resp.json()["data"]["items"]
    assert len(visible_items) == 1
    assert visible_items[0]["title"] == "Visible document"

    keyword_resp = await async_client.get("/api/v1/admin/docs?keyword=hidden", headers=headers)
    assert keyword_resp.status_code == 200
    keyword_items = keyword_resp.json()["data"]["items"]
    assert len(keyword_items) == 1
    assert keyword_items[0]["title"] == "Hidden document"


@pytest.mark.asyncio
async def test_admin_docs_batch_delete(async_client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}

    first = await async_client.post(
        "/api/v1/admin/docs",
        headers=headers,
        json={
            "title": "Batch one",
            "content": "doc one",
            "is_visible": True,
            "sort_order": 1,
        },
    )
    second = await async_client.post(
        "/api/v1/admin/docs",
        headers=headers,
        json={
            "title": "Batch two",
            "content": "doc two",
            "is_visible": True,
            "sort_order": 2,
        },
    )
    first_id = first.json()["data"]["id"]
    second_id = second.json()["data"]["id"]
    missing_id = str(uuid4())

    response = await async_client.post(
        "/api/v1/admin/docs/batch-delete",
        headers=headers,
        json={"ids": [first_id, second_id, missing_id, first_id]},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "requested": 3,
        "deleted": 2,
        "missing": 1,
        "missing_ids": [missing_id],
        "failed_ids": [],
    }

    list_resp = await async_client.get("/api/v1/admin/docs", headers=headers)
    assert list_resp.status_code == 200
    remaining_ids = {item["id"] for item in list_resp.json()["data"]["items"]}
    assert first_id not in remaining_ids
    assert second_id not in remaining_ids


@pytest.mark.asyncio
async def test_user_docs_uses_cached_payload(async_client, admin_token, monkeypatch):
    headers = {"Authorization": f"Bearer {admin_token}"}
    cached_payload = {
        "items": [
            {
                "id": "cache-doc-1",
                "title": "Cached doc",
                "content": "from cache",
                "is_visible": True,
                "sort_order": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ]
    }

    async def fake_get_json(key: str):
        assert key == docs_route.VISIBLE_DOCS_CACHE_KEY
        return cached_payload

    async def fake_set_json(*args, **kwargs):  # pragma: no cover
        raise AssertionError("cache hit should not repopulate Redis")

    monkeypatch.setattr(docs_route, "get_json", fake_get_json)
    monkeypatch.setattr(docs_route, "set_json", fake_set_json)

    response = await async_client.get("/api/v1/docs", headers=headers)
    assert response.status_code == 200
    assert response.json()["data"] == cached_payload


@pytest.mark.asyncio
async def test_admin_docs_mutations_invalidate_visible_cache(
    async_client, admin_token, monkeypatch
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    deleted_keys: list[str] = []

    async def fake_delete(key: str):
        deleted_keys.append(key)
        return 1

    monkeypatch.setattr(admin_docs, "delete", fake_delete)

    create_resp = await async_client.post(
        "/api/v1/admin/docs",
        headers=headers,
        json={
            "title": "Invalidate me",
            "content": "body",
            "is_visible": True,
            "sort_order": 1,
        },
    )
    assert create_resp.status_code == 201
    doc_id = create_resp.json()["data"]["id"]

    update_resp = await async_client.patch(
        f"/api/v1/admin/docs/{doc_id}",
        headers=headers,
        json={"title": "Invalidate me too"},
    )
    assert update_resp.status_code == 200

    delete_resp = await async_client.delete(f"/api/v1/admin/docs/{doc_id}", headers=headers)
    assert delete_resp.status_code == 200

    batch_first = await async_client.post(
        "/api/v1/admin/docs",
        headers=headers,
        json={
            "title": "Batch invalidate one",
            "content": "body",
            "is_visible": True,
            "sort_order": 2,
        },
    )
    batch_second = await async_client.post(
        "/api/v1/admin/docs",
        headers=headers,
        json={
            "title": "Batch invalidate two",
            "content": "body",
            "is_visible": True,
            "sort_order": 3,
        },
    )
    batch_resp = await async_client.post(
        "/api/v1/admin/docs/batch-delete",
        headers=headers,
        json={
            "ids": [batch_first.json()["data"]["id"], batch_second.json()["data"]["id"]],
        },
    )
    assert batch_resp.status_code == 200

    assert deleted_keys == [
        admin_docs.VISIBLE_DOCS_CACHE_KEY,
        admin_docs.VISIBLE_DOCS_CACHE_KEY,
        admin_docs.VISIBLE_DOCS_CACHE_KEY,
        admin_docs.VISIBLE_DOCS_CACHE_KEY,
        admin_docs.VISIBLE_DOCS_CACHE_KEY,
        admin_docs.VISIBLE_DOCS_CACHE_KEY,
    ]
