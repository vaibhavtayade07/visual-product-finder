"""Cost guard (§6.4): atomic per-tenant monthly counter, block/warn modes."""
from conftest import error_code, make_jpeg, new_session, search


def counter_for(sql, slug: str) -> int:
    row = sql.execute(
        """SELECT queries FROM usage_counters
           WHERE tenant_id = (SELECT id FROM tenants WHERE slug=%s)
           ORDER BY period DESC LIMIT 1""",
        (slug,),
    ).fetchone()
    return int(row["queries"]) if row else 0


def test_query_cap_blocks_fail_closed(client, make_tenant, sql):
    slug = make_tenant({"query_cap_monthly": 2, "rate_limit": {"ip_per_min": 100}})
    assert search(client, make_jpeg(seed=1), tenant=slug, session_id=new_session()).status_code == 202
    assert search(client, make_jpeg(seed=2), tenant=slug, session_id=new_session()).status_code == 202

    blocked = search(client, make_jpeg(seed=3), tenant=slug, session_id=new_session())
    assert blocked.status_code == 429
    assert error_code(blocked) == "QUERY_CAP_REACHED"
    assert blocked.json()["error"]["details"]["cap"] == 2

    # fail-closed accounting: the blocked attempt was still counted (conservative)
    assert counter_for(sql, slug) == 3

    # subsequent attempts stay blocked; counter keeps counting attempts
    again = search(client, make_jpeg(seed=4), tenant=slug, session_id=new_session())
    assert again.status_code == 429
    assert counter_for(sql, slug) == 4


def test_cost_guard_warn_allows_overflow(client, make_tenant, sql):
    slug = make_tenant({"query_cap_monthly": 1, "cost_guard": "warn",
                        "rate_limit": {"ip_per_min": 100}})
    assert search(client, make_jpeg(seed=1), tenant=slug, session_id=new_session()).status_code == 202
    over = search(client, make_jpeg(seed=2), tenant=slug, session_id=new_session())
    assert over.status_code == 202, "warn mode proceeds past the cap"
    assert counter_for(sql, slug) == 2


def test_cap_is_per_tenant(client, make_tenant):
    tight = make_tenant({"query_cap_monthly": 1, "rate_limit": {"ip_per_min": 100}})
    roomy = make_tenant({"query_cap_monthly": 50, "rate_limit": {"ip_per_min": 100}})
    assert search(client, make_jpeg(seed=1), tenant=tight, session_id=new_session()).status_code == 202
    assert search(client, make_jpeg(seed=2), tenant=tight, session_id=new_session()).status_code == 429
    # a different tenant is unaffected
    assert search(client, make_jpeg(seed=3), tenant=roomy, session_id=new_session()).status_code == 202
