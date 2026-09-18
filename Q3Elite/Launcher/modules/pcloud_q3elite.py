#!/usr/bin/env python3
import argparse, hashlib, json, os, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path, PurePosixPath

API="https://eapi.pcloud.com"
CORE="Quake 3 Arena/Quake 3 Elite"
MAPS="Quake 3 Arena/Maps"
MUSIC="Quake 3 Arena/z-Music-Playlist-by-Mus1n.pk3dir"
LOCAL_MUSIC_PREFIX="baseq3/mods/osp/z-Music-Playlist-by-Mus1n.pk3dir/"
AUTOEXEC="baseq3/mods/OSP/autoexec.cfg"
UA="Q3Elite-Launcher-Step14"
TIMEOUT=30

def root():
    here=Path(__file__).resolve().parent
    for p in (here,*here.parents):
        if (p/"Q3Elite").is_dir() and (p/"baseq3").is_dir(): return p
    return Path(__file__).resolve().parents[3]
ROOT=root()
MANIFEST=ROOT/"Q3Elite"/"Manifest.json"
PCLOUD_DCONF=ROOT/"Q3Elite"/"Launcher"/"download_confs"/"Quake 3 Elite pcloud.dconf"

def load_pcloud_code():
    """Read pCloud public-link code from Quake 3 Elite pcloud.dconf."""
    if not PCLOUD_DCONF.is_file():
        raise FileNotFoundError(f"Missing pCloud config: {PCLOUD_DCONF}")

    text=PCLOUD_DCONF.read_text(encoding="utf-8-sig")

    # Accept a normal pCloud public URL anywhere in the dconf, e.g.
    # https://e.pcloud.link/publink/show?code=...
    import re
    match=re.search(r"(?:[?&]|\b)code=([A-Za-z0-9_-]+)",text,re.IGNORECASE)
    if match:
        return match.group(1)

    # Also accept a bare share code on a non-comment line.
    for raw in text.splitlines():
        line=raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.fullmatch(r"[A-Za-z0-9_-]{20,}",line):
            return line

    raise RuntimeError(
        "Cannot find pCloud public-link code in:\n"
        f"{PCLOUD_DCONF}"
    )

CODE=load_pcloud_code()

def norm(s): return str(PurePosixPath(str(s).replace("\\","/").lstrip("/")))
def sha(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(4*1024*1024),b""): h.update(b)
    return h.hexdigest()
def api(name, **params):
    url=f"{API}/{name}?"+urllib.parse.urlencode(params)
    req=urllib.request.Request(url,headers={"User-Agent":UA})
    with urllib.request.urlopen(req,timeout=TIMEOUT) as r:
        d=json.loads(r.read().decode())
    if d.get("result",0): raise RuntimeError(f"pCloud {d.get('result')}: {d.get('error')}")
    return d
_TREE_CACHE=None
def tree(refresh=False):
    global _TREE_CACHE
    if refresh or _TREE_CACHE is None:
        _TREE_CACHE=api("showpublink",code=CODE)
    return _TREE_CACHE
def find(remote):
    parts=norm(remote).split("/")
    cur=tree().get("metadata",{})
    if parts and str(cur.get("name","")).casefold()==parts[0].casefold(): parts.pop(0)
    for part in parts:
        items=cur.get("contents",[])
        nxt=next((x for x in items if str(x.get("name","")).casefold()==part.casefold()),None)
        if nxt is None:
            avail=", ".join(str(x.get("name","")) for x in items[:15])
            raise FileNotFoundError(f"{remote}\nMissing: {part}\nAvailable: {avail}")
        cur=nxt
    return cur
def resolve(remote):
    e=find(remote)
    if e.get("isfolder"): raise IsADirectoryError(remote)
    fid=e.get("fileid")
    d=api("getpublinkdownload",code=CODE,fileid=fid)
    hosts=d.get("hosts") or []
    if not hosts or not d.get("path"): raise RuntimeError("No pCloud download URL")
    return {"remote":norm(remote),"fileid":fid,"size":int(e.get("size",0)),
            "url":f"https://{hosts[0]}{d['path']}"}
def remote_for(local,is_map=False):
    p=norm(local)

    # Music is deliberately stored outside the remote Quake 3 Elite folder,
    # while locally it belongs inside baseq3/mods/osp/.
    if p.casefold().startswith(LOCAL_MUSIC_PREFIX.casefold()):
        suffix=p[len(LOCAL_MUSIC_PREFIX):]
        return f"{MUSIC}/{suffix}"

    if is_map:
        pre="baseq3/maps/"
        if not p.casefold().startswith(pre): raise ValueError("Map path must start baseq3/maps/")
        return f"{MAPS}/{p[len(pre):]}"
    return f"{CORE}/{p}"

def list_folder(remote, recursive=False):
    """Return pCloud metadata for files in a public-link folder."""
    folder=find(remote)
    if not folder.get("isfolder"):
        raise NotADirectoryError(remote)

    result=[]
    def walk(node, prefix):
        for item in node.get("contents",[]):
            name=str(item.get("name",""))
            rel=f"{prefix}/{name}" if prefix else name
            if item.get("isfolder"):
                if recursive:
                    walk(item,rel)
            else:
                result.append({
                    "name":name,
                    "relative":norm(rel),
                    "fileid":item.get("fileid"),
                    "size":int(item.get("size",0)),
                })
    walk(folder,"")
    return result

def resolve_entry(entry):
    """Resolve a file metadata entry without another showpublink tree fetch."""
    fid=entry.get("fileid")
    if not fid:
        raise RuntimeError(f"No pCloud fileid for {entry.get('name','file')}")
    d=api("getpublinkdownload",code=CODE,fileid=fid)
    hosts=d.get("hosts") or []
    if not hosts or not d.get("path"):
        raise RuntimeError("No pCloud download URL")
    return {
        **entry,
        "url":f"https://{hosts[0]}{d['path']}",
    }

def music_files():
    """Dynamic contents of the separately hosted Music Playlist folder."""
    return list_folder(f"{MUSIC}/music", recursive=True)
def expected(local):
    d=json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
    wanted=norm(local).casefold()
    for p,h in d.get("files",{}).items():
        if norm(p).casefold()==wanted: return str(h).lower()
    raise KeyError(f"Not in Manifest.json: {local}")
def human(n):
    for unit in ("B","KB","MB","GB"):
        if n<1024 or unit=="GB": return f"{n:.1f} {unit}" if unit!="B" else f"{int(n)} B"
        n/=1024
def download(info,dest,want):
    dest.parent.mkdir(parents=True,exist_ok=True)
    part=dest.with_name(dest.name+".part"); part.unlink(missing_ok=True)
    req=urllib.request.Request(info["url"],headers={"User-Agent":UA})
    got=0; start=time.monotonic()
    with urllib.request.urlopen(req,timeout=TIMEOUT) as r, part.open("wb") as f:
        while True:
            b=r.read(1024*1024)
            if not b: break
            f.write(b); got+=len(b)
            if info["size"]:
                print(f"\r{got*100/info['size']:6.2f}%  {human(got)} / {human(info['size'])}",end="",flush=True)
    print()
    if info["size"] and got!=info["size"]: raise RuntimeError(f"Size mismatch: {got} != {info['size']}")
    actual=sha(part)
    if actual.lower()!=want.lower(): raise RuntimeError(f"SHA-256 mismatch\nExpected: {want}\nActual:   {actual}")
    os.replace(part,dest)
    sec=max(time.monotonic()-start,.001)
    print("Saved:",dest); print("SHA-256:",actual); print("Average:",human(got/sec)+"/s")
def test(local,is_map=False,force=False):
    local=norm(local); dest=ROOT/Path(local); want=expected(local)
    print("="*70); print("Q3Elite pCloud test"); print("="*70)
    print("ROOT:",ROOT); print("Local:",dest); print("Expected:",want)
    if dest.is_file() and not force:
        actual=sha(dest); print("Local SHA:",actual)
        if actual.lower()==want.lower():
            print("[OK] Current. Nothing downloaded."); return
        print("[UPDATE] Hash differs.")
    elif not dest.exists(): print("[MISSING]")
    else: print("[FORCE]")
    remote=remote_for(local,is_map); print("pCloud:",remote)
    info=resolve(remote)
    print("fileid:",info["fileid"]); print("size:",info["size"],f"({human(info['size'])})")
    download(info,dest,want); print("[OK] Download and verification completed.")
def main():
    ap=argparse.ArgumentParser()
    sp=ap.add_subparsers(dest="cmd",required=True)
    sp.add_parser("autoexec"); sp.add_parser("update")
    c=sp.add_parser("core"); c.add_argument("path"); c.add_argument("--force",action="store_true")
    m=sp.add_parser("map"); m.add_argument("path"); m.add_argument("--force",action="store_true")
    r=sp.add_parser("resolve"); r.add_argument("path"); r.add_argument("--map",action="store_true")
    a=ap.parse_args()
    if a.cmd=="autoexec": test(AUTOEXEC,False,True)
    elif a.cmd=="update": test(AUTOEXEC)
    elif a.cmd=="core": test(a.path,False,a.force)
    elif a.cmd=="map": test(a.path,True,a.force)
    else:
        remote=remote_for(a.path,a.map); print("pCloud:",remote)
        print(json.dumps(resolve(remote),indent=2))
if __name__=="__main__":
    try: main()
    except urllib.error.HTTPError as e: print(f"[HTTP ERROR] {e.code}: {e.reason}"); raise SystemExit(2)
    except urllib.error.URLError as e: print(f"[NETWORK ERROR] {e.reason}"); raise SystemExit(3)
    except Exception as e: print("[ERROR]",e); raise SystemExit(1)
