# Example backlog: Hello API clean-up

Text above the first item reaches no worker. Use it for notes to yourself. Each item is one pull request. A Verify line runs under bash in a fresh checkout of the base branch after the merge, again and again until it passes or its `Wait.` deadline (in minutes) runs out. Read `.claude/skills/herdr-loop/references/authoring.md` before you write your own.

## EX-01 — `GET /health` reports the running version

**Scope.** `app/routes/health.py` answers `GET /health` with `{"status": "ok"}`. It becomes `{"status": "ok", "version": "<version>"}`, where `<version>` is the `version` field of `pyproject.toml`, read once at start-up. Nothing else in the answer changes, and the status code stays 200. `tests/test_health.py` asserts the old body and is updated with this change. Paths: `app/routes/health.py`, `tests/test_health.py`.

**Accept.**
1. With the build served on the item's port, `curl -s localhost:<port>/health | jq -r .version` prints the `version` from `pyproject.toml` (it prints `null` at `main` today).
2. `curl -s -o /dev/null -w '%{http_code}' localhost:<port>/health` prints `200`.
3. `pytest tests/test_health.py -q` exits 0.

**Depends.** none.

**Verify.** `curl -fsS https://api.example.com/health | jq -e '.version != null'`
Wait. 15

## EX-02 — `terraform validate` prints no deprecation warning for the users table

**Scope.** `infra/dynamodb.tf` declares the users table with one global secondary index whose keys are given as `hash_key`/`range_key`, which the AWS provider now deprecates. What it becomes: two `key_schema` blocks. **The live table must not change:** a replace, a destroy, or any change to the index is a stop. Paths: `infra/dynamodb.tf` only.

**Accept.**
1. `terraform -chdir=infra validate -no-color 2>&1 | grep -c 'is deprecated'` prints `0` (it prints `2` at `main` today), and `validate` reports the configuration valid.
2. `grep -c 'key_schema' infra/dynamodb.tf` prints `2` (it prints `0` today).
3. `terraform -chdir=infra fmt -check -recursive` exits 0.
4. The pull request's Terraform plan shows the table with no change.

**Depends.** none.

**Floor.** sonnet: it rewrites the declaration of a live table; a wrong block could plan a replace.

**Verify.** `gh run list -R your-name/hello-api -w deploy.yml -c "$(git rev-parse HEAD)" --json conclusion -q '.[0].conclusion' | grep -qx success`
Wait. 20

## EX-03 — The README names the health route's new field

**Scope.** `README.md`'s "Health check" section shows the old `/health` answer. It becomes the answer EX-01 gives, with the `version` field. Nothing else in the README changes. Paths: `README.md` only.

**Accept.**
1. `grep -c '"version"' README.md` prints `1` (it prints `0` today).

**Depends.** EX-01.

**Verify.** `grep -q '"version"' README.md`
Wait. 5
