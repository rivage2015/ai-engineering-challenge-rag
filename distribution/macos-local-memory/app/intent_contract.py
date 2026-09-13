"""User-reviewed answer requirements, bound to a source generation."""
import hashlib
import hmac
import json
import secrets
import time

# Never use a browser-visible CSRF token as the contract signing key.
SIGNING_KEY = secrets.token_urlsafe(32)


def draft_intent(query, scope):
    """Clarify granularity without inventing domain-specific answer slots.

    Keep the complete question in the goal; requirements describe coverage,
    not a preselected answer structure. The user reviews both before search.
    """
    if scope == 'workflow':
        return (
            '元の質問で指定した対象・範囲について、一連の手順と条件分岐を知りたい。'
            '範囲を指定している場合は、その外まで広げない。\n元の質問：' + query,
            '質問で指定した範囲の手順と、資料に記載された順序・条件分岐',
        )
    return query, '知りたいことに対する、資料で裏付けられる具体的な回答'


def make_contract(query, goal, requirements, revision):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000:
        raise ValueError("質問は1〜2000文字で入力してください。")
    if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 3000:
        raise ValueError("知りたいことを1〜3000文字で入力してください。")
    items = [s.strip() for s in requirements.splitlines() if s.strip()]
    if not 1 <= len(items) <= 8 or any(len(s) > 300 for s in items):
        raise ValueError("必要な内容は1〜8項目、各300文字以内で入力してください。")
    return {"version": 1, "question": query.strip(), "goal": goal.strip(),
            "requirements": items, "revision": revision,
            "expires_at": int(time.time()) + 1800}


def seal(contract, secret):
    payload = json.dumps(contract, ensure_ascii=False, sort_keys=True)
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload, signature


def read_signed(payload, signature, secret):
    if not isinstance(payload, str) or len(payload) > 16000:
        raise ValueError("確認内容が無効です。")
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
        raise ValueError("確認内容が更新されています。もう一度完成形を確認してください。")
    return json.loads(payload)


def verify(payload, signature, secret, revision):
    contract = read_signed(payload, signature, secret)
    if contract["expires_at"] < time.time() or contract["revision"] != revision:
        raise ValueError("資料または確認の有効期限が更新されました。もう一度確認してください。")
    return contract


def search_question(contract):
    return (contract["goal"] + "\n回答に必要な内容：\n"
            + "\n".join(f"{i+1}. {s}" for i, s in enumerate(contract["requirements"]))
            + "\n各項目を根拠付きで説明してください。資料にない手順や分岐は補わず、"
            "確認できない項目を明示してください。")


def check_coverage(contract, answer, verdict):
    """An evaluator may only mark coverage using exact nonempty answer quotes.

    This checks completeness, not factual correctness; evidence audit remains
    authoritative and cannot be overridden by this result.
    """
    supplied = verdict.get("items", []) if isinstance(verdict, dict) else []
    if not isinstance(supplied, list):
        supplied = []
    result = []
    for i, requirement in enumerate(contract["requirements"]):
        matches = [x for x in supplied if isinstance(x, dict) and type(x.get("index")) is int and x["index"] == i]
        item = matches[0] if len(matches) == 1 else {}
        quote = item.get("quote", "")
        covered = (item.get("covered") is True and isinstance(quote, str)
                   and bool(quote.strip()) and quote in answer)
        result.append({"requirement": requirement, "covered": covered,
                       "quote": quote if covered else ""})
    return {"complete": all(x["covered"] for x in result), "items": result}
