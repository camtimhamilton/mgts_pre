from app.knowledge import load_knowledge, load_mapping


if __name__ == "__main__":
    kb, mapping = load_knowledge(), load_mapping()
    print(f"PASS: KB {kb.version}; {len(kb.factors)} factors; {len(kb.rules)} rules; "
          f"weights={sum(kb.policy.weights.values())}; mapping={mapping.version}; hash={kb.content_hash}")
