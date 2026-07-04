#!/usr/bin/env python3
import sys
sys.path.insert(0, '/home/pwn/workspace/research/swarm-knowledge')
from src import SwarmDB, search
db = SwarmDB('/home/pwn/workspace/research/swarm-knowledge/swarm_knowledge.db')
for q in ['auth', 'login', 'SSO', 'OAuth', 'token', 'bancoplata', 'prime', 'fr']:
    r = search(db, q, level_min=1)
    for item in r:
        content = item.get('content','')
        if content:
            print(content[:500])
            print('---')
db.close()
