from src.memory import load_custom_rules as l
rules = l()
print(len(rules))
print([r.id for r in rules[-5:]])
