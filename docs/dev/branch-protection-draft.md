# Branch protection 推奨案（草案・未適用）

CI の実 run で下記 check 名が確認できてから `main` に適用する。先に未存在の check を登録しない。

- Required status checks: `python-test` / `lint` / `secret-scan`
- Require a pull request before merging（approval 1 件以上）
- Require branches to be up to date
- Force push / 削除を禁止
- fork PR では secret を使わない（`pull_request` のみ、`pull_request_target` は使わない）
