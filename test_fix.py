import json, subprocess, os, sys
sys.path.insert(0, '/home/hatch/workspace/vpin-bridge-fix')
import bridge

ENV = dict(os.environ)
ENV['PC_BRIDGE_URL'] = 'https://pin.compact.synology.me'
ENV['PYTHONPATH'] = '/opt/muse/skills/custom-connector-setup/bin'

def run_vpin(*args):
    r = subprocess.run(
        ['python3', '/home/hatch/workspace/custom-skills/local-vpin-bridge/bin/vpin', *args],
        capture_output=True, text=True, env=ENV, timeout=120)
    return r.stdout

# 1. Fetch the REAL games export (raw CSV) and test both maps.
games = json.loads(run_vpin('games'))
games_text = games.get('raw', '')
print("games export chars:", len(games_text))
lp_map = bridge.parse_last_played_map(games_text)
names_map = bridge.parse_game_names_map(games_text)
print("games in lastPlayed map:", len(lp_map))
print("games in names map:", len(names_map))
# Status text test: gameId 104 should be Bram Stokers Dracula
print("gameId 104 name:", repr(names_map.get(104)))
print("104 resolves correctly:", names_map.get(104) == 'Bram Stokers Dracula')
# Mass Effect 313: empty lastPlayed, real modified date
me = lp_map.get(313)
print("Mass Effect 313 map entry:", me)
print("Mass Effect lastPlayed empty:", me[0] == '' if me else 'n/a')
print("Mass Effect modified parsed:", me[2] if me else 'n/a')

# 2. Test the sort with constructed highscores CSV in the REAL format,
# using real gameIds/names and the real lastPlayed/modified values.
# (The live bridge returns parsed JSON, not raw CSV, so build the CSV.)
hs_csv = (
    "gameId;gameDisplayName;createdAt;playerInitials1;score1;playerInitials2;score2;\n"
    "313;Mass Effect 1.7.3;Nov 8, 2026, 6:20:36 PM;SLL;7,500,000,000;\n"
    "104;Bram Stokers Dracula;Nov 8, 2026, 6:20:36 PM;\"CD \";300,000,000;\n"
    "182;Ace Of Speed;Nov 8, 2026, 6:20:36 PM;DAN;7,000,000;\n"
    "258;Aaron Spelling;Nov 8, 2026, 6:20:36 PM;AAA;175,000,000;\n"
    "148;Iron Maiden Legacy of the Beast;Nov 8, 2026, 6:20:36 PM;BBB;250,000,000;\n"
    "309;Borderlands;Nov 8, 2026, 6:20:36 PM;CCC;128,450,600;\n"
)
entries = bridge.parse_highscores_plain(hs_csv, lp_map)
print("parsed entries:", len(entries) if entries else 0)
if entries:
    order = [(e['gameId'], e['text'], e['lastPlayed']) for e in entries]
    print("sort order:")
    for gid, name, lp in order:
        print("  ", gid, '|', name, '| lastPlayed', repr(lp))
    ids = [e['gameId'] for e in entries]
    print("Bram Stokers Dracula (104) first:", ids[0] == 104)
    print("Ace Of Speed (182) second:", ids[1] == 182)
    print("Mass Effect (313) third (modified fallback):", ids[2] == 313)
    print("Mass Effect lastPlayed text still empty:", entries[2]['lastPlayed'] == '')
    print("No _sort_key leak:", all('_sort_key' not in e for e in entries))
    # Borderlands has no lastPlayed and (per earlier data) no modified either -> sorts last
    print("Borderlands (309) last:", ids[-1] == 309)
