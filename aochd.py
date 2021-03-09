# coding: utf-8

"""
aochd.jpでリプレイ見ながら埋まり待ちするための待合室BOT
AndroidスマホのPydroid3で稼働する都合で1ファイルでとても長い。
"""

import json
import os
import pickle
import select
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import traceback
import pprint

# irc
IRC_SERVER = ("aochd.jp", 6667)
NICKNAME = "rakou_bot"
#CHANNEL = "#rakou実験中"
CHANNEL = "#AoCHD"
ENCODING = "utf-8"

# socket
BUFSIZE = 32768
POLL_INTERVAL = 0.5

# serialized
ROOMS_PICKLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aochd_rooms.pickle")

# room
MAX_ROOM_NUM = 1000

USAGE = """リプレイを見てても乗り遅れないためのゲーム募集システムです。チーム分けはaochd.jpのサイトで手動入力してね。
コマンド一覧:
    [ホスト向け]
        mkroom@: 部屋作成。mkroom@部屋名。mkroom@2000以下、mkroom@無制限、など。省略可。の、ぬけ、解散、強制解散、全角スペース、の文字は使用できない。
        解散: 解散。濃い文字(PRIVMSG)で参加者が表示される（設定していれば各自ベルが鳴る）kaisanでも可。
        ggwp: 解散。薄い文字(NOTICE)で参加者が表示される（ベルが鳴らない）
        chcap@: 定員を設定。デフォルトは8が設定済み。chcap@6など。
        chrn@: （mkroomで書いた）部屋の説明文を更新。chrn@やっぱり1600以下、など
        chhost@: ホスト交代。chhost@名前
        yyk@: リプレイを我慢してAoCロビーを開き、IRCにいない新規さんなどを拾うときに使う。yyk@名前。「の」の代行。
        kick@: キックです。kick@名前
        chancel@: キックと同等。kickでは良心がとがめるとき。chancel@名前
    [一般向け]
        の: 参加。複数の部屋があるときは、の＠部屋番号、の＠１、no@2など。部屋を移るのにも使える。
        ぬけ: 部屋を抜ける。nukeでも可。
        rooms: 募集中の部屋一覧（既存のgameコマンドとは異なる）
        iam@名前: rakou → _rakou などニックネームが変わって正しく操作できないとき一時的に使用。
        強制解散@: ホストが寝たとき、解散忘れなどのときに使用。強制解散＠部屋番号、force_breakup@部屋番号。ほっといても24時間で消える。
        rbhelp: 上記の説明文を表示。"""

# \x02太字\x02
# \x03文字色,背景色text\x03
# 白 00
# 紫 06
# 青 12
IN = "\x02\x0312,00[IN]\x03\x02"
OUT = "\x02\x0306,00[OUT]\x03\x02"

def inout(inout, nickname):
  return "%s %s" % (inout, nickname)

class Room(object):
  def __init__(self, host):
    self.name = ""
    self.host = host
    self.members = [host]
    self.capacity = 8
    self.announce_interval = 60 * 15
    self.last_announced_at = time.time()
    self.created_at = time.time()
    self.number = None

class Manager(object):
  def __init__(self):
    self.exit = threading.Event()
    self.continue_ = threading.Event()
    self.continue_.set()
    self.irc = None
    self.rooms = []
    self.member_aliases = {}
    if os.path.exists(ROOMS_PICKLE):
      with open(ROOMS_PICKLE, "rb") as f:
        self.rooms = pickle.loads(f.read())

  def _search(self, nickname):
    for room in self.rooms:
      if nickname in room.members:
        return room
    return None

  def _set_room_number(self):
    for i, room in enumerate(self.rooms):
      room.number = i + 1

  def _save(self):
    with open(ROOMS_PICKLE, "wb") as f:
      f.write(pickle.dumps(self.rooms))
    #(mode, tmp) = tempfile.mkstemp()
    #with open(tmp, "wb") as f:
    #  f.write(pickle.dumps(self.rooms))
    #  f.close()
    # Windowsだと上書きせずWindowsError 32
    #shutil.move(tmp, ROOMS_PICKLE)

  def _get_args(self, privmsg):
    return privmsg["text"].split("@", 1)[1]

  def _print_room(self, room, bell, comment=""):
    s = "[%(room_number)d] %(room_name)s %(members)s @%(nokori)d %(comment)s" % {
        "room_number": room.number,
        "room_name": room.name,
        "members": json.dumps(room.members),
        "nokori": room.capacity - len(room.members),
        "comment": comment,
        }
    if bell:
      self._privmsg(s)
    else:
      self._notice(s)

  def _mkroom(self, privmsg):
    room = self._search(privmsg["nickname"])
    if room:
      self._print_room(room, bell=False, comment="入室済み")
      return
    room = Room(host=privmsg["nickname"])
    room.name = self._get_args(privmsg) 
    self.rooms.append(room)
    self._set_room_number()
    self._print_room(room, bell=False, comment=inout(IN, privmsg["nickname"]))
    self._save()

  def _breakup(self, privmsg, bell):
    room = self._search(privmsg["nickname"])
    if room is None:
      self._notice("対象の部屋が見つかりません")
      return
    if room.host != privmsg["nickname"]:
      self._notice("ホストでない方は強制解散を使ってください")
      return
    if bell:
      comment = "ホストに解散されました"
    else:
      comment = "解散。お疲れさまでした"
    self._print_room(room, bell=bell, comment=comment)
    self.rooms.pop(self.rooms.index(room))
    self._save()
  
  def _force_breakup(self, privmsg):
    room_number = self._get_args(privmsg)
    try:
      room_number = int(room_number)
    except ValueError:
      self._notice("部屋番号の指定が誤っています")
      return
    try:
      room = list(filter(lambda room: room.number==room_number, self.rooms))[0]
    except IndexError:
      self._notice("対象の部屋が見つかりません")
      return
    self.rooms.pop(self.rooms.index(room))
    self._save()
    self._print_room(room, bell=True, comment="強制解散しました")

  def _chcap(self, privmsg):
    room = self._search(privmsg["nickname"])
    if room is None:
      self._notice("対象の部屋が見つかりません")
      return
    if room.host != privmsg["nickname"]:
      self._notice("ホストだけが定員を変更できます")
      return
    cap = self._get_args(privmsg)
    try:
      cap = int(cap)
      if not cap in range(2,9):
        raise Exception
    except ValueError:
      self._notice("定員の指定が誤っています")
    except Exception:
      self._notice("定員の指定が誤っています。2～8の数字を入力してください")
      return
    if cap < len(room.members):
      self._notice("入室済み人数よりも定員を少なく設定することはできません")
      return
    before = room.capacity
    after = cap
    room.capacity = cap
    self._save()
    comment = "%dから%dに定員を変更しました" % (before, after)
    self._print_room(room, bell=False, comment=comment)

  def _chrn(self, privmsg):
    room = self._search(privmsg["nickname"])
    if room is None:
      self._notice("対象の部屋が見つかりません")
      return
    if room.host != privmsg["nickname"]:
      self._notice("ホストだけが部屋名を変更できます")
      return
    before = room.name
    after = self._get_args(privmsg)
    room.name = after
    self._save()
    comment = "%s から %s に部屋名を変更しました" % (before, after)
    self._print_room(room, bell=False, comment=comment)

  def _enter_validated(self, room, nickname):
    if room.capacity <= len(room.members):
      self._print_room(room, bell=False, comment="満員で入れません")
      return
    room.members.append(nickname)
    self._save()
    if room.capacity == len(room.members):
      bell = True
      self._privmsg("埋まり。まもなく開始です。ホストの方はチーム分けをお願いします。GL")
    else:
      bell = False
    self._print_room(room, bell=bell, comment=inout(IN, nickname))

  def _chhost(self, privmsg):
    room = self._search(privmsg["nickname"])
    if room.host != privmsg["nickname"]:
      self._notice("ホストだけがホスト交代を実行できます")
      return
    name = self._get_args(privmsg)
    if not name in room.members:
      self._notice("部屋にいない人とはホスト交代できません")
      return
    before = room.host
    after = name
    room.host = name
    room.members.insert(0, room.members.pop(room.members.index(name)))
    self._save()
    self._print_room(room, bell=False, comment="%sさんから%sさんにホスト交代しました" % (before, after))

  def _yyk(self, privmsg):
    room = self._search(privmsg["nickname"])
    if room is None:
      self._notice("対象の部屋が見つかりません。ホストになれば使用できます")
      return
    if room.capacity < len(room.members):
      self._notice("満員です")
    if room.host != privmsg["nickname"]:
      self._notice("ホストだけが予約を実行できます")
      return
    name = self._get_args(privmsg)
    self._enter_validated(room, name)

  def _kick(self, privmsg, kick):
    room = self._search(privmsg["nickname"])
    if room is None:
      self._notice("対象の部屋が見つかりません")
      return
    if room.host != privmsg["nickname"]:
      self._notice("ホストだけがキックを実行できます")
      return
    name = self._get_args(privmsg)
    if name == room.host:
      self._notice("ホストをキックすることはできません")
      return
    try:
      room.members.pop(room.members.index(name))
    except:
      self._notice("対象が見つかりません")
      return
    self._save()
    comment = inout(OUT, name) + " ホストにより%sされました" % "キック" if kick else "キャンセル"
    self._print_room(room, bell=False, comment=comment)

  def _enter(self, privmsg):
    if not self.rooms:
      self._notice("現在、部屋はありません")
      return
    if not "@" in privmsg["text"]:
      rooms_not_full = list( filter(lambda room: 0 < (room.capacity - len(room.members)), self.rooms) )
      print(rooms_not_full[0].host)
      if len(rooms_not_full) == 1:
        room = rooms_not_full[0]
        if privmsg["nickname"] in room.members:
          self._print_room(room, bell=False, comment="入室済み")
          return
        self._enter_validated(room, privmsg["nickname"])
        return
      if 1 < len(rooms_not_full):
        self._notice("入れる部屋が複数あるので@で区切って部屋番号を指定してください。の＠部屋番号")
        return
    else:
      target_number = self._get_args(privmsg)
      try:
        target_number = int(target_number)
      except ValueError:
        self._notice("部屋番号の指定が誤っています")
        return
      try:
        target_room = list(filter(lambda room: room.number==target_number, self.rooms))[0]
      except:
        self._notice("対象の部屋が見つかりません")
        return
      current_room = self._search(privmsg["nickname"])
      if current_room:
        if current_room is target_room:
          self._print_room(current_room, bell=False, comment="入室済み")
          return
        if (target_room.capacity - len(target_room.members)) == 0:
          self._print_room(target_room, bell=False, comment="満員で入れません")
          return
      self._leave(privmsg)
      self._enter_validated(target_room, privmsg["nickname"])

  def _leave(self, privmsg):
    room = self._search(privmsg["nickname"])
    if room is None:
      self._notice("あなたは部屋に入っていません")
      return
    if room.host == privmsg["nickname"]:
      self._breakup(privmsg, bell=True)
      return
    room.members.pop(room.members.index(privmsg["nickname"]))
    self._save()
    self._print_room(room, bell=False, comment=inout(OUT, privmsg["nickname"]))

  def _list_rooms(self):
    if not self.rooms:
      self._notice("現在、部屋はありません")
      return
    for room in self.rooms:
      self._print_room(room, bell=False)
      time.sleep(0.5)

  def _iam(self, privmsg):
    name = self._get_args(privmsg)
    self.member_aliases.update({privmsg["nickname"]: name})
    self._notice("%sさんは%sさんなんだね" % (name, privmsg["nickname"]))

  def _usage(self):
    for line in USAGE.split("\n"):
      self._notice(line)
      time.sleep(0.5)

  def _manager_shell(self, s):
    if s is None:
      return
    if s.startswith("PING"):
      self._pong(s.split()[1])
      #self._privmsg("[test] PONG send")
      return
    privmsg = self._try_parse_privmsg(s)
    if privmsg is None:
      return

    if privmsg["text"].startswith("iam@"):
      self._iam(privmsg)
    truename = self.member_aliases.get(privmsg["nickname"])
    if truename:
      privmsg["nickname"] = truename

    if privmsg["text"].startswith("mkroom@"):
      self._mkroom(privmsg)
    if privmsg["text"] == "kaisan":
      self._breakup(privmsg, bell=True)
    if privmsg["text"] == "ggwp":
      self._breakup(privmsg, bell=False)
    if privmsg["text"].startswith("chcap@"):
      self._chcap(privmsg)
    if privmsg["text"].startswith("chrn@"):
      self._chrn(privmsg)
    if privmsg["text"].startswith("chhost@"):
      self._chhost(privmsg)
    if privmsg["text"].startswith("yyk@"):
      self._yyk(privmsg)
    if privmsg["text"].startswith("kick@"):
      self._kick(privmsg, kick=True)
    if privmsg["text"].startswith("cancel@"):
      self._kick(privmsg, kick=False)
    if privmsg["text"].startswith("no"):
      try:
        ( ["no"] + ["no@%d" % i for i in range(1, MAX_ROOM_NUM)] ).index(privmsg["text"])
      except ValueError: 
        return
      else:
        self._enter(privmsg)
    if privmsg["text"] == "nuke":
      self._leave(privmsg)
    if privmsg["text"] == "rooms":
      self._list_rooms()
    if privmsg["text"].startswith("force_breakup@"):
      self._force_breakup(privmsg)
    if privmsg["text"] == "rbhelp":
      self._usage()
    print(privmsg["nickname"])
    print(privmsg["text"])

  def _try_parse_privmsg(self, s):
    if not s.startswith(":"):
      return None
    if not "PRIVMSG" in s:
      return None
    if not ("%s :" % CHANNEL) in s:
      return None
    try:
      (user, text) = s[1:].split(" PRIVMSG %s :" % CHANNEL, 1)
      nickname = user.split("!")[0]
      text = self._text_normalizer(text)
      return {"nickname": nickname, "text": text}
    except ValueError:
      return None
    except IndexError:
      return None

  def _select(self, socket_, timeout=0):
      try:
        r, w, e = select.select([socket_], [], [], timeout)
      except socket.error:
        self.continue_.clear()
        return None
      if r:
        return r[0]
      else:
        return None

  def _send(self, msg):
    if self.irc:
      try:
        self.irc.send( (msg + "\n").encode(ENCODING) )
      except socket.error:
        self.continue_.clear()

  def _recv(self):
    if self.irc:
      try:
        s = self.irc.recv(BUFSIZE)
        print(s)
        s = s.decode(ENCODING)
        print(s)
      except socket.error:
        self.continue_.clear()
        s = None
      except UnicodeDecodeError:
        s = None
      return s

  def _quit(self):
    self._send("QUIT")
    self.irc.close()

  def _pong(self, s):
    self._send("PONG %s" % s)

  def _privmsg(self, s):
    self._send("PRIVMSG %s :%s" % (CHANNEL, s))

  def _notice(self, s):
    self._send("NOTICE %s :%s" % (CHANNEL, s))

  def _text_normalizer(self, text):
    for i, wide_digit in enumerate(["０", "１", "２", "３", "４", "５", "６", "７", "８", "９"]):
      text = text.replace(wide_digit, str(i))
    text = text.replace("＠", "@")\
        .replace("　", " ")\
        .replace("の", "no")\
        .replace("ぬけ", "nuke")\
        .replace("強制解散", "force_breakup")\
        .replace("解散", "kaisan")\
        .strip()
    return text

  def _session_initialize(self):
    try:
      self.continue_.set()
      self.irc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      self.irc.connect(IRC_SERVER)
      self.irc.setblocking(0)
      self._send( "USER %s 0 * %s" % (NICKNAME, NICKNAME) )
      self._send( "NICK %s" % NICKNAME)
      if not self._select(self.irc, 120):
        print("initialize timeout")
        self.continue_.clear()
        return
      noop = self.irc.recv(BUFSIZE)
      print(noop)
      time.sleep(0.5)
      self._send( "JOIN %s" % CHANNEL)
    except socket.error:
      self.continue_.clear()

  def exit_switch(self):
    while True:
      if input("exitと入力したら終了: ") == "exit":
        self.continue_.clear()
        self.exit.set()
        break
      else:
        continue

  def session(self):
    self._session_initialize()
    try:
      while self.continue_.is_set():
        if self._select(self.irc, POLL_INTERVAL):
          s = self._recv()
          self._manager_shell(s)
    except Exception as e:
      import traceback
      print("Exception")
      t, v, tb = sys.exc_info()
      pprint.pprint(traceback.format_exception(t,v,tb))
      pprint.pprint(traceback.format_tb(e.__traceback__))
    finally:
      self._quit()


def join_threads():
  """メインスレッドで、他のスレッドの終了を待つ"""
  if threading.current_thread() is threading.main_thread():
    for t in threading.enumerate():
      if t is not threading.main_thread():
        t.join()
  else:
    pass

def session(manager):
  threading.Thread(target=manager.session).start()
  threading.Thread(target=manager.exit_switch).start()
  while manager.continue_.is_set():
    time.sleep(1)
  join_threads()

def main():
  manager = Manager()
  while True:
    session(manager)
    if manager.exit.is_set():
      break
    time.sleep(60) # exit以外で落ちたら60秒後に再接続
  sys.exit(0)

main()
