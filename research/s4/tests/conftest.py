from pathlib import Path
import sys
R=Path(__file__).resolve().parents[3]
for p in [R/'src',R/'scripts',Path(__file__).resolve().parents[1]]:sys.path.insert(0,str(p))
