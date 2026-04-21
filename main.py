# ↓ これ残す
import os
import uuid
import logging
import markdown
import re
import unicodedata
import base64
import hashlib
import secrets
from flask import Flask, request, render_template, redirect, url_for, send_from_directory, session, flash, abort, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_, and_, cast
from sqlalchemy.types import Integer
from flask_migrate import Migrate
from functools import wraps
from flask_cors import CORS
from pathlib import Path
from dotenv import load_dotenv
from urllib.parse import urlparse
load_dotenv(Path(__file__).with_name(".env"))
# Supabase クライアント（自作ファイル）だけ使う
from supabase_client import supabase








app = Flask(__name__)

secret = os.getenv("SECRET_KEY")
if not secret:
    raise RuntimeError("SECRET_KEY is missing. Check /srv/encard/app/.env")
app.config["SECRET_KEY"] = secret
app.secret_key = secret
basedir = os.path.abspath(os.path.dirname(__file__))

basedir = os.path.abspath(os.path.dirname(__file__))

IS_LOCAL = os.getenv("ENV") == "local"

if IS_LOCAL:
    UPLOAD_DIR = os.path.join(basedir, "static", "uploads")
else:
    UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/srv/encard/uploads")




os.makedirs(UPLOAD_DIR, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///images.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper())
db = SQLAlchemy(app)
migrate = Migrate(app, db)

origins_env = os.getenv("CORS_ORIGINS", "")
origins = [o.strip() for o in origins_env.split(",") if o.strip()]
if not origins:
    origins = ["https://dmencard.site", "https://www.dmencard.site"]

CORS(
    app,
    resources={r"/*": {"origins": origins}},
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
)
app.logger.warning("CORS enabled for: %s", origins)

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def to_int(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip().replace(',', '')
    if s == '':
        return None
    return int(s) if s.lstrip('-').isdigit() else None

_NORM_PAT = re.compile(
    r"[\s\u3000,，、。・!！?？()（）\[\]【】{}「」『』＝=<>＜＞/／\\\-ー–—:;.'\"…]+"
)

# 空白だけ消す（記号だけ検索用）
_SPACE_PAT = re.compile(r"[\s\u3000]+")

def normalize_text(s: str) -> str:
    """空白・記号・カンマなどを消して小文字化（通常検索用）"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    s = _NORM_PAT.sub("", s)
    return s

def normalize_keep_symbols(s: str) -> str:
    """空白だけ消して小文字化（記号検索用）"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    s = _SPACE_PAT.sub("", s)
    return s
def build_page_items(page: int, pages: int, window: int = 2):
    """
    例: pages=20, page=10 なら
    [1, None, 8, 9, 10, 11, 12, None, 20]
    """
    if pages <= 1:
        return [1]

    items = set()
    items.add(1)
    items.add(pages)

    for p in range(page - window, page + window + 1):
        if 1 <= p <= pages:
            items.add(p)

    items = sorted(items)

    out = []
    prev = None
    for p in items:
        if prev is not None and p - prev > 1:
            out.append(None)  # … の代わり
        out.append(p)
        prev = p
    return out

def local_path_from_image_url(image_url: str) -> str | None:
    """
    image_url が /uploads/xxx.jpg または https://dmencard.site/uploads/xxx.jpg のとき
    実ファイルパス /srv/encard/uploads/xxx.jpg を返す。それ以外は None。
    """
    if not image_url:
        return None

    # 絶対URLでも相対URLでも対応
    u = urlparse(image_url)
    path = u.path if u.scheme else image_url  # 相対ならそのまま

    if not path.startswith("/uploads/"):
        return None

    filename = path.split("/uploads/", 1)[1]
    if not filename:
        return None

    return os.path.join(app.config["UPLOAD_FOLDER"], filename)

def safe_unlink(path: str | None) -> bool:
    if not path:
        return False
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except Exception as e:
        app.logger.warning("Failed to delete file %s: %s", path, e)
    return False

def normalize_image_url_for_env(image_url: str) -> str:
    if not image_url:
        return image_url

    # 本番では一切いじらない
    if not IS_LOCAL:
        return image_url

    u = urlparse(image_url)
    path = u.path if u.scheme else image_url  # 絶対URL→パス、相対はそのまま

    # /uploads/xxx ならローカルの /uploads/xxx に統一
    if path.startswith("/uploads/"):
        return path

    return image_url

REGULATION_LABELS = {
    0: "Not Restricted",
    1: "Restricted",
    2: "Premium Restricted",
    3: "Combo Restricted",
    4: "Unlimited",
}

def get_regulation_label(value):
    try:
        return REGULATION_LABELS.get(int(value), "Unknown")
    except (TypeError, ValueError):
        return "Unknown"

# 管理者認証デコレーター
def admin_required(func):
    @wraps(func)
    def decorated_view(*args, **kwargs):
        if not session.get('admin'):
            flash('管理者ログインが必要です')
            return redirect(url_for('admin_login'))
        return func(*args, **kwargs)
    return decorated_view

# Special user_id used for admin-created decks
ADMIN_USER_ID = '__admin__'
ADMIN_DISPLAY_NAME = 'official deck'

# ユーザー認証デコレーター（将来のデッキビルダー等に使用）
def login_required(func):
    @wraps(func)
    def decorated_view(*args, **kwargs):
        # Allow both regular users and admin sessions
        if not session.get('user_id') and not session.get('admin'):
            flash('Please log in to access this feature.')
            return redirect(url_for('user_login'))
        return func(*args, **kwargs)
    return decorated_view

# PKCE ヘルパー
def _generate_pkce_pair():
    """PKCE code_verifier と code_challenge を生成する"""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
    return code_verifier, code_challenge

# カードモデル
class Card(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    image_url = db.Column(db.String, nullable=True)
    image_url2 = db.Column(db.String, nullable=True)
    image_public_id = db.Column(db.String(255))
    image_public_id2 = db.Column(db.String(255))
    name_ja = db.Column(db.String(255))
    name_ja_kana = db.Column(db.String(255))
    name_en = db.Column(db.String(255))
    card_type = db.Column(db.String(255))
    civilization = db.Column(db.String(255))
    cost = db.Column(db.Integer)
    text_ja = db.Column(db.Text)
    text_en = db.Column(db.Text)
    power = db.Column(db.Integer)
    tribe = db.Column(db.String(255))
    illustrator = db.Column(db.String(255))
    reference = db.Column(db.String(512))
    note = db.Column(db.Text)
    twin_name_ja = db.Column(db.String(255))
    twin_name_ja_kana = db.Column(db.String(255))
    twin_name_en = db.Column(db.String(255))
    twin_card_type = db.Column(db.String(255))
    twin_civilization = db.Column(db.String(255))
    twin_cost = db.Column(db.Integer)
    twin_text_ja = db.Column(db.Text)
    twin_text_en = db.Column(db.Text)
    twin_power = db.Column(db.Integer)
    twin_tribe = db.Column(db.String(255))

with app.app_context():
    db.create_all()

# ログインユーザー情報を全テンプレートに渡す
@app.context_processor
def inject_current_user():
    user_id = session.get('user_id')
    email = session.get('user_email', '')
    username = ''
    if user_id:
        username = _get_username(user_id)
        if not username:
            username = email
    return {
        'current_user': {
            'id': user_id,
            'email': email,
            'username': username,
            'is_authenticated': bool(user_id),
        }
    }

@app.get("/__cors_test")
def __cors_test():
    return "ok"

@app.route('/')
def index():
    page = request.args.get('page', 1, type=int)
    per_page = 20

    try:
        response = supabase.table("Cards").select("*").order("id", desc=True).execute()
        all_cards = response.data or []

        # ページネーション処理
        total = len(all_cards)
        start = (page - 1) * per_page
        end = start + per_page
        paginated_cards = all_cards[start:end]

        # ローカル用に image_url を補正（paginated_cards 作成後にやる）
        for c in paginated_cards:
            c["image_url"]  = normalize_image_url_for_env(c.get("image_url"))
            c["image_url2"] = normalize_image_url_for_env(c.get("image_url2"))
            c["regulation_label"] = get_regulation_label(c.get("regulation_type"))
            
        has_prev = page > 1
        has_next = end < total
        prev_num = page - 1
        next_num = page + 1
        pages = (total + per_page - 1) // per_page
        page_items = build_page_items(page, pages, window=2)

        # Compute custom card types (non-preset) for the Card Type dropdown
        _PRESET_TYPES_IDX = [
            "Creature", "Evolution Creature", "Spell", "Tamaseed", "Field",
            "GR", "Dragheart", "Psychic", "Castle", "Cross Gear", "Aura", "Duelist",
        ]
        _space_idx = re.compile(r"\s+")
        def _norm_ct_idx(s):
            s = unicodedata.normalize("NFKC", (s or "")).strip().lower()
            return _space_idx.sub(" ", s)
        _preset_norms_idx = {_norm_ct_idx(p) for p in _PRESET_TYPES_IDX}
        _dt_set_idx = set()
        for c in all_cards:
            for key in ("card_type", "twin_card_type"):
                v = _norm_ct_idx(c.get(key))
                if v and v not in _preset_norms_idx:
                    _dt_set_idx.add(v)
        detail_types = sorted(_dt_set_idx, key=lambda x: x.lower())

        return render_template(
            'index.html',
            cards=paginated_cards,
            page=page,
            has_prev=has_prev,
            has_next=has_next,
            prev_num=prev_num,
            next_num=next_num,
            pages=pages,
            page_items=page_items,
            search_query="",
            detail_types=detail_types,
            selected_detail_types=[],
            is_admin=bool(session.get('admin')),
        )

    except Exception as e:
        return f"トップページの取得でエラーが発生しました: {e}", 500





@app.route('/search')
def search():
    query_text = request.args.get('query', '').strip()
    tribe = request.args.get('tribe', '').strip()
    # 受け取り方を変更：複数文明・色タイプ・AND/OR
    civilizations_raw = request.args.get('civilization', '')  # "Fire,Water" みたいなCSV
    selected_civs = [c.strip() for c in civilizations_raw.split(',') if c.strip()]
    color_mode   = request.args.get('color_mode', 'all')      # "all" | "mono" | "multi"
    mode         = request.args.get('mode', 'or')
    cost_min = request.args.get('cost_min', type=int)
    cost_max = request.args.get('cost_max', type=int)
    power_min = request.args.get('power_min', type=int)
    power_max = request.args.get('power_max', type=int)
    card_types_raw = request.args.get('card_type', '')  # 例: "Spell,Creature_all,other"
    selected_card_types = [x.strip() for x in card_types_raw.split(',') if x.strip()]
    card_types_detail_raw = request.args.get('card_type_detail', '')  # 例: "MyTypeA,MyTypeB"
    selected_detail_types = [x.strip() for x in card_types_detail_raw.split(',') if x.strip()]
    regulation_type = request.args.get('regulation_type', type=int)
    hof_only = request.args.get('hof_only') == '1'
    search_in_raw = request.args.getlist('search_in')
    search_in = search_in_raw if search_in_raw else ['name']
    page = request.args.get('page', 1, type=int)
    per_page = 20


    try:
        # 全件取得（本番では条件に応じて最適化するのが理想）
        response = supabase.table("Cards").select("*").execute()
        all_cards = response.data or []
# 既存タイプ（カテゴリ）一覧
        PRESET_TYPES = [
          "Creature",
          "Evolution Creature",
          "Spell",
          "Tamaseed",
          "Field",
          "GR",
          "Dragheart",
          "Psychic",
          "Castle",
          "Cross Gear",
          "Aura",
          "Duelist",
        ]

        _space = re.compile(r"\s+")

        def normalize_ct(s: str) -> str:
            s = unicodedata.normalize("NFKC", (s or "")).strip().lower()
            s = _space.sub(" ", s)
            return s

        PRESET_NORMS = {normalize_ct(p) for p in PRESET_TYPES}

        def is_exact_preset(ct: str) -> bool:
            """ct が既存カテゴリに “完全一致” するか"""
            return normalize_ct(ct) in PRESET_NORMS


# detailに出す「独自カードタイプ」を集計（main/twin 両方見る）
        detail_types_set = set()
        for c in all_cards:
            for key in ("card_type", "twin_card_type"):
                v = normalize_ct(c.get(key))
                if v and not is_exact_preset(v):
                    detail_types_set.add(v)

        detail_types = sorted(detail_types_set, key=lambda x: x.lower())

        # 絞り込み（Python側で処理）
        # 絞り込み（Python側で処理）
        if query_text:
            qn = normalize_text(query_text)

    # 検索対象のテキストを作る（search_in に従う）
            def card_blob_norm(card: dict) -> str:
                parts = []
                if 'name' in search_in:
                    parts += [
                        card.get('name_ja'),
                        card.get('name_ja_kana'),
                        card.get('name_en'),
                        card.get('twin_name_ja'),
                        card.get('twin_name_ja_kana'),
                        card.get('twin_name_en'),
                    ]
                if 'text' in search_in:
                    parts += [
                        card.get('text_ja'),
                        card.get('text_en'),
                        card.get('twin_text_ja'),
                        card.get('twin_text_en'),
                        card.get('note'),
                    ]
                if 'illustrator' in search_in:
                    parts += [card.get('illustrator')]
                joined = " ".join([p for p in parts if p])
                return normalize_text(joined)

    # 記号検索用（空白だけ消す）
            def card_blob_raw(card: dict) -> str:
                parts = [
                    card.get('name_ja'),
                    card.get('name_en'),
                    card.get('twin_name_ja'),
                    card.get('twin_name_ja_kana'),
                    card.get('twin_name_en'),
                    card.get('name_ja_kana'),
                ]
                joined = " ".join([p for p in parts if p])
                return normalize_keep_symbols(joined)

            if qn:
        # 1) 通常検索：記号/空白を無視して検索
                all_cards = [c for c in all_cards if qn in card_blob_norm(c)]
            else:
        # 2) 入力が記号だけっぽい：記号そのまま検索
                qraw = normalize_keep_symbols(query_text)
                if qraw:
                    all_cards = [c for c in all_cards if qraw in card_blob_raw(c)]
        # qraw も空（空白だけ）なら何もしない（=フィルタしない）

        if tribe:
            all_cards = [
                card for card in all_cards
                if tribe.lower() in (card.get('tribe') or '').lower()
            ]

        if regulation_type is not None:
            all_cards = [
                card for card in all_cards
                if to_int(card.get('regulation_type')) == regulation_type
            ]

        if hof_only:
            all_cards = [
                card for card in all_cards
                if to_int(card.get('regulation_type')) not in (None, 0)
            ]
        def is_multi(s: str) -> bool:
            return '/' in s if s else False

        def is_mono(s: str) -> bool:
            return (s or '') != '' and '/' not in s

        def civ_tokens(s: str):
            """'Fire/Water' -> ['Fire','Water']（全角スラッシュも考慮）"""
            if not s:
                return []
            s = s.replace('／', '/')
            return [p.strip() for p in s.split('/') if p.strip()]

        def field_matches_target(field_value: str, target: str, color_mode: str) -> bool:
            """1つの文明フィールド（メイン or ツイン）が target を満たすか"""
            if not field_value:
                return False
            # 色タイプでふるい分け
            if color_mode == 'mono' and not is_mono(field_value):
                return False
            if color_mode == 'multi' and not is_multi(field_value):
                return False
            # トークンで厳密一致（大文字小文字は無視）
            tokens = civ_tokens(field_value)
            t = target.lower()
            return any(tok.lower() == t for tok in tokens) if tokens else (t in field_value.lower())

        def card_matches_target(card: dict, target: str, color_mode: str) -> bool:
            """カード（メイン or ツインどちらか）が target を満たすか"""
            return (
                field_matches_target(card.get('civilization'),     target, color_mode) or
                field_matches_target(card.get('twin_civilization'), target, color_mode)
            )

        # 選択文明あり → AND/OR で絞る
        if selected_civs:
            if mode == 'and':
                all_cards = [
                    c for c in all_cards
                    if all(card_matches_target(c, civ, color_mode) for civ in selected_civs)
                ]
            else:  # 'or'
                all_cards = [
                    c for c in all_cards
                    if any(card_matches_target(c, civ, color_mode) for civ in selected_civs)
                ]
        else:
            # 文明は未選択だが単色／多色だけ指定された場合
            if color_mode in ('mono', 'multi'):
                def card_is_mono(card):
                    main = card.get('civilization') or ''
                    twin = card.get('twin_civilization') or ''
                    # どちらかが多色なら多色扱い
                    if is_multi(main) or is_multi(twin):
                        return False
                    # どちらかに文明が入っていて、両方とも多色ではない
                    return bool(main or twin)

                def card_is_multi(card):
                    return is_multi(card.get('civilization') or '') or is_multi(card.get('twin_civilization') or '')

                if color_mode == 'mono':
                    all_cards = [c for c in all_cards if card_is_mono(c)]
                else:
                    all_cards = [c for c in all_cards if card_is_multi(c)]

        #card type
        def card_type_match_preset(card: dict, token: str) -> bool:
            """token(選択肢)がカードにマッチするか。main/twin両方チェック"""
            main = normalize_ct(card.get("card_type")).lower()
            twin = normalize_ct(card.get("twin_card_type")).lower()

            def any_field(pred):
                return pred(main) or pred(twin)

            if token == "Creature_all":
                return any_field(lambda s: "creature" in s)

            if token == "Creature_only":
                return any_field(lambda s: s == "creature")

            if token == "other":
        # 既存カテゴリに当てはまらない＆空じゃない
                return any_field(lambda s: (s != "" and not is_exact_preset(s)))

    # 通常カテゴリ: その単語を含む
            t = token.lower()
            return any_field(lambda s: t in s)

        def card_type_match_detail(card: dict, detail: str) -> bool:
            """detail（独自タイプ）完全一致でマッチ（main/twin）"""
            d = normalize_ct(detail).lower()
            main = normalize_ct(card.get("card_type")).lower()
            twin = normalize_ct(card.get("twin_card_type")).lower()
            return (main == d) or (twin == d)

# ここが本体：選択がある場合だけ絞る
        if selected_card_types or selected_detail_types:
            all_cards = [
                c for c in all_cards
                if (
                    any(card_type_match_preset(c, t) for t in selected_card_types)
                    or any(card_type_match_detail(c, d) for d in selected_detail_types)
                )
            ]


        # これを cost フィルタの所に置き換え
        if cost_min is not None or cost_max is not None:
          def to_int_safe(v):
            try:
                return int(v) if v is not None and str(v).strip() != "" else None
            except (TypeError, ValueError):
                return None

          def in_range(v):
            if v is None:
               return False
            if cost_min is not None and v < cost_min:
               return False
            if cost_max is not None and v > cost_max:
               return False
            return True

          all_cards = [
             card for card in all_cards
              if in_range(to_int_safe(card.get('cost'))) or
              in_range(to_int_safe(card.get('twin_cost')))
            ]


        # ... cost のフィルタの後あたりに追加
        if power_min is not None:
            all_cards = [
              c for c in all_cards
              if ((to_int(c.get('power')) is not None and to_int(c.get('power')) >= power_min) or
                  (to_int(c.get('twin_power')) is not None and to_int(c.get('twin_power')) >= power_min))
            ]

        if power_max is not None:
            all_cards = [
              c for c in all_cards
              if ((to_int(c.get('power')) is not None and to_int(c.get('power')) <= power_max) or
                  (to_int(c.get('twin_power')) is not None and to_int(c.get('twin_power')) <= power_max))
            ]


        # ソート（ID降順）
        all_cards.sort(key=lambda c: c.get("id", 0), reverse=True)
        
   


        # ページネーション処理
        total = len(all_cards)
        start = (page - 1) * per_page
        end = start + per_page
        paginated_cards = all_cards[start:end]

        for c in paginated_cards:
            c["image_url"]  = normalize_image_url_for_env(c.get("image_url"))
            c["image_url2"] = normalize_image_url_for_env(c.get("image_url2"))
            c["regulation_label"] = get_regulation_label(c.get("regulation_type"))

        has_prev = page > 1
        has_next = end < total
        prev_num = page - 1
        next_num = page + 1
        pages = (total + per_page - 1) // per_page

        def build_page_items(current: int, total: int, window: int = 2):
            """
            例: total=20, current=10 -> [1, None, 8, 9, 10, 11, 12, None, 20]
            None は "..." 表示用
            """
            if total <= 1:
                return [1]

            items = []

            # まず最初
            items.append(1)

            # current周辺の範囲
            start = max(2, current - window)
            end   = min(total - 1, current + window)

            # 1 と start の間が空くなら ...
            if start > 2:
                items.append(None)

            # 途中のページ
            for p in range(start, end + 1):
                items.append(p)

            # end と total の間が空くなら ...
            if end < total - 1:
                items.append(None)

            # 最後
            items.append(total)

            return items
        page_items = build_page_items(page, pages, window=2)

        # テンプレート描画
        return render_template(
            "index.html",
            cards=paginated_cards,
            page=page,
            has_prev=has_prev,
            has_next=has_next,
            prev_num=prev_num,
            next_num=next_num,
            pages=pages,
            search_query=query_text,
            selected_card_types=selected_card_types,
            selected_detail_types=selected_detail_types,
            detail_types=detail_types,
            page_items=page_items,
            is_admin=bool(session.get('admin')),
        )

    except Exception as e:
        return f"検索中にエラーが発生しました: {e}", 500


@app.route('/card/<int:id>')
def card_detail(id):
    try:
        resp = supabase.table("Cards").select("*").eq("id", id).single().execute()
        card = resp.data
        if not card:
            abort(404)
        card["image_url"] = normalize_image_url_for_env(card.get("image_url"))
        card["image_url2"] = normalize_image_url_for_env(card.get("image_url2"))
        note_html = markdown.markdown(card.get('note') or '')
        card["regulation_label"] = get_regulation_label(card.get("regulation_type"))

        # Check if this card belongs to a group
        card_group_info = None
        try:
            gm_res = supabase.table("card_group_members").select("group_id, position").eq("card_id", id).execute()
            gm_rows = gm_res.data or []
            if gm_rows:
                group_id = gm_rows[0]["group_id"]
                all_m_res = supabase.table("card_group_members").select("card_id, position").eq("group_id", group_id).order("position").execute()
                all_members_raw = all_m_res.data or []
                member_card_ids = [m["card_id"] for m in all_members_raw]
                mc_res = supabase.table("Cards").select("id, name_en, name_ja").in_("id", member_card_ids).execute()
                mc_map = {c["id"]: c for c in (mc_res.data or [])}
                group_members = []
                for m in all_members_raw:
                    ci = mc_map.get(m["card_id"], {})
                    group_members.append({
                        "card_id": m["card_id"],
                        "position": m["position"],
                        "name": ci.get("name_en") or ci.get("name_ja") or str(m["card_id"]),
                    })
                card_group_info = {"group_id": group_id, "members": group_members}
        except Exception:
            pass

        return render_template('card_detail.html', card=card, note_html=note_html,
                               card_group_info=card_group_info)
    except Exception as e:
        return f"カード取得エラー: {e}", 500


@app.route('/admin')
@admin_required
def admin_dashboard():
    page = request.args.get('page', 1, type=int)
    per_page = 10
    search_query = request.args.get('query', '').strip()

    try:
        query = supabase.table("Cards").select("*")

        if search_query:
            pattern = f"%{search_query}%"
            # or_ は「1つの文字列」にカンマ区切りで条件を書く
            or_filters = ",".join([
                f"name_ja.ilike.{pattern}",
                f"name_en.ilike.{pattern}",
                f"tribe.ilike.{pattern}",
                f"civilization.ilike.{pattern}",
                f"text_ja.ilike.{pattern}",
                f"text_en.ilike.{pattern}",
                f"illustrator.ilike.{pattern}",
            ])
            query = query.or_(or_filters)   # ← ここがポイント

        # ページネーション
        query = query.order("id", desc=True).range((page - 1) * per_page,
                                                   page * per_page - 1)
        response = query.execute()
        cards = response.data or []

    except Exception as e:
        flash(f"エラーが発生しました: {e}")
        cards = []

    return render_template("admin_dashboard.html",
                           cards=cards,
                           search_query=search_query)





@app.route('/upload', methods=['GET', 'POST'])
@admin_required
def upload_file():
    if request.method == 'POST':
        # ---- 文字列で受ける ----
        name_ja          = request.form.get('name_ja')
        name_ja_kana     = request.form.get('name_ja_kana')
        name_en          = request.form.get('name_en')
        card_type        = request.form.get('card_type')
        civilization     = request.form.get('civilization')
        cost_raw         = request.form.get('cost')          # ← 文字列
        text_ja          = request.form.get('text_ja')
        text_en          = request.form.get('text_en')
        power_raw        = request.form.get('power')         # ← 文字列
        tribe            = request.form.get('tribe')
        regulation_type_raw = request.form.get('regulation_type', '0')
        illustrator      = request.form.get('illustrator')
        reference        = request.form.get('reference')
        note             = request.form.get('note')

        twin_name_ja         = request.form.get('twin_name_ja')
        twin_name_ja_kana    = request.form.get('twin_name_ja_kana')
        twin_name_en         = request.form.get('twin_name_en')
        twin_card_type       = request.form.get('twin_card_type')
        twin_civilization    = request.form.get('twin_civilization')
        twin_cost_raw        = request.form.get('twin_cost')     # ← 文字列
        twin_text_ja         = request.form.get('twin_text_ja')
        twin_text_en         = request.form.get('twin_text_en')
        twin_power_raw       = request.form.get('twin_power')    # ← 文字列
        twin_tribe           = request.form.get('twin_tribe')

        # ---- 数値化 ----
        cost       = to_int(cost_raw)
        power      = to_int(power_raw)
        twin_cost  = to_int(twin_cost_raw)
        twin_power = to_int(twin_power_raw)

        # ---- 画像アップロード ----
        file  = request.files.get('file')
        file2 = request.files.get('file2')
        image_url = image_url2 = None
        image_public_id = image_public_id2 = None

        try:
            regulation_type = int(regulation_type_raw)
        except (TypeError, ValueError):
            regulation_type = 0

        def save_upload(f):
            if not f or not f.filename:
                return None

            if not allowed_file(f.filename):
                abort(400, "invalid file type")

            ext = f.filename.rsplit(".", 1)[1].lower()
            filename = f"{uuid.uuid4().hex}.{ext}"
            save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)

            f.save(save_path)
            return f"/uploads/{filename}"
        image_url  = save_upload(file)
        image_url2 = save_upload(file2)
        # ---- Supabase へ挿入 ----
        data = {
          "name_ja": name_ja,
          "name_ja_kana": name_ja_kana,
          "name_en": name_en,
          "card_type": card_type,
          "civilization": civilization,
          "cost": cost,
          "text_ja": text_ja,
          "text_en": text_en,
          "power": power,
          "tribe": tribe,
          "regulation_type": regulation_type,
          "illustrator": illustrator,
          "reference": reference,
          "note": note,
          "twin_name_ja": twin_name_ja,
          "twin_name_ja_kana": twin_name_ja_kana,
          "twin_name_en": twin_name_en,
          "twin_card_type": twin_card_type,
          "twin_civilization": twin_civilization,
          "twin_cost": twin_cost,
          "twin_text_ja": twin_text_ja,
          "twin_text_en": twin_text_en,
          "twin_power": twin_power,
          "twin_tribe": twin_tribe,
          "image_url": image_url,
          "image_url2": image_url2,
          "image_public_id": image_public_id,
          "image_public_id2": image_public_id2,
        }

        try:
            response = supabase.table("Cards").insert(data).execute()
            flash("カードを登録しました。")
        except Exception as e:
            import traceback; traceback.print_exc()
            flash(f"Supabase登録に失敗: {e}")
            return redirect(url_for('upload_file'))

        return redirect(url_for('admin_dashboard'))

    return render_template('upload.html')





@app.route('/card/<int:id>/edit', methods=['GET', 'POST']) 
@admin_required
def edit_card(id):
    if request.method == 'POST':
        updated_data = {
            "name_ja": request.form.get('name_ja'),
            "name_ja_kana": request.form.get('name_ja_kana'),
            "name_en": request.form.get('name_en'),
            "card_type": request.form.get('card_type'),
            "civilization": request.form.get('civilization'),
            # ↓ 安全に数値化
            "cost": to_int(request.form.get('cost')),
            "text_ja": request.form.get('text_ja'),
            "text_en": request.form.get('text_en'),
            "power": to_int(request.form.get('power')),
            "tribe": request.form.get('tribe'),
            "regulation_type": to_int(request.form.get('regulation_type')) or 0,           
            "illustrator": request.form.get('illustrator'),
            "reference": request.form.get('reference'),
            "note": request.form.get('note'),
            "twin_name_ja": request.form.get('twin_name_ja'),
            "twin_name_ja_kana": request.form.get('twin_name_ja_kana'),
            "twin_name_en": request.form.get('twin_name_en'),
            "twin_card_type": request.form.get('twin_card_type'),
            "twin_civilization": request.form.get('twin_civilization'),
            "twin_cost": to_int(request.form.get('twin_cost')),
            "twin_text_ja": request.form.get('twin_text_ja'),
            "twin_text_en": request.form.get('twin_text_en'),
            "twin_power": to_int(request.form.get('twin_power')),
            "twin_tribe": request.form.get('twin_tribe')
        }

        file = request.files.get('file')
        file2 = request.files.get('file2')
        def save_upload(f):
            if not f or not f.filename:
                return None
            if not allowed_file(f.filename):
                abort(400, "invalid file type")

            ext = f.filename.rsplit(".", 1)[1].lower()
            filename = f"{uuid.uuid4().hex}.{ext}"
            save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            f.save(save_path)
            return f"/uploads/{filename}"
        old = supabase.table("Cards").select("image_url, image_url2").eq("id", id).single().execute().data or {}

        if file and file.filename:
            safe_unlink(local_path_from_image_url(old.get("image_url")))
            updated_data["image_url"] = save_upload(file)

            updated_data["image_public_id"] = None
        if file2 and file2.filename:
            safe_unlink(local_path_from_image_url(old.get("image_url2")))
            updated_data["image_url2"] = save_upload(file2)
            updated_data["image_public_id2"] = None
        try:
            supabase.table("Cards").update(updated_data).eq("id", id).execute()
            flash("カード情報を更新しました")
        except Exception as e:
            flash(f"更新に失敗しました: {e}")
            return redirect(url_for('edit_card', id=id))

        return redirect(url_for('admin_dashboard'))

    # GET はそのまま
    try:
        response = supabase.table("Cards").select("*").eq("id", id).single().execute()
        card = response.data
        return render_template('edit_card.html', card=card)
    except Exception as e:
        return f"カード取得エラー: {e}", 500




# ===== ユーザー認証ルート =====

@app.route('/user/login', methods=['GET', 'POST'])
def user_login():
    if session.get('user_id'):
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        if not email or not password:
            flash('Please enter your email address and password.')
            return render_template('user_login.html')
        try:
            res = supabase.auth.sign_in_with_password({"email": email, "password": password})
            if res.user and res.session:
                session['user_id'] = res.user.id
                session['user_email'] = res.user.email
                session['access_token'] = res.session.access_token
                flash('You are now logged in.')
                return redirect(url_for('index'))
            else:
                flash('Login failed. Please try again.')
        except Exception as e:
            app.logger.warning("Login error: %s", e)
            flash('Incorrect email address or password.')
    return render_template('user_login.html')


@app.route('/user/signup', methods=['GET', 'POST'])
def user_signup():
    if session.get('user_id'):
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')
        if not email or not password:
            flash('Please enter your email address and password.')
            return render_template('user_signup.html')
        if password != password2:
            flash('Passwords do not match.')
            return render_template('user_signup.html')
        if len(password) < 6:
            flash('Password must be at least 6 characters.')
            return render_template('user_signup.html')
        try:
            res = supabase.auth.sign_up({"email": email, "password": password})
            if res.user:
                flash('A confirmation email has been sent. Please check your inbox to activate your account.')
                return redirect(url_for('user_login'))
            else:
                flash('Registration failed. Please try again.')
        except Exception as e:
            app.logger.warning("Signup error: %s", e)
            flash(f'Registration failed: {e}')
    return render_template('user_signup.html')


@app.route('/user/logout')
def user_logout():
    try:
        token = session.get('access_token')
        if token:
            supabase.auth.sign_out()
    except Exception:
        pass
    session.pop('user_id', None)
    session.pop('user_email', None)
    session.pop('access_token', None)
    flash('You have been logged out.')
    return redirect(url_for('index'))


@app.route('/user/google-login')
def google_login():
    """Google OAuth ログイン（PKCE フロー）"""
    code_verifier, code_challenge = _generate_pkce_pair()
    session['pkce_verifier'] = code_verifier

    supabase_url = os.getenv("SUPABASE_URL", "").rstrip('/')
    # コールバック先をホストに合わせて動的に生成
    callback_url = request.url_root.rstrip('/') + '/auth/callback'

    from urllib.parse import urlencode
    params = {
        "provider": "google",
        "redirect_to": callback_url,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    oauth_url = f"{supabase_url}/auth/v1/authorize?" + urlencode(params)
    return redirect(oauth_url)


@app.route('/auth/callback')
def auth_callback():
    """Supabase OAuth コールバック"""
    code = request.args.get('code')
    error = request.args.get('error')

    if error:
        flash(f'Google sign-in was cancelled: {request.args.get("error_description", error)}')
        return redirect(url_for('user_login'))

    if code:
        code_verifier = session.pop('pkce_verifier', None)
        try:
            params = {"auth_code": code}
            if code_verifier:
                params["code_verifier"] = code_verifier
            res = supabase.auth.exchange_code_for_session(params)
            if res.user and res.session:
                session['user_id'] = res.user.id
                session['user_email'] = res.user.email
                session['access_token'] = res.session.access_token
                flash('Signed in with Google.')
                return redirect(url_for('index'))
        except Exception as e:
            app.logger.warning("OAuth callback error: %s", e)
            flash('Google authentication failed. Please try again.')
            return redirect(url_for('user_login'))

    # コード無しの場合（フラグメントトークン対応）
    return render_template('auth_callback.html')


@app.route('/auth/set-session', methods=['POST'])
def auth_set_session():
    """JS経由でアクセストークンをサーバーセッションに保存する（implicit flowフォールバック）"""
    data = request.get_json(silent=True) or {}
    access_token = data.get('access_token') or request.form.get('access_token')
    if not access_token:
        return jsonify({'error': 'no token'}), 400
    try:
        user_res = supabase.auth.get_user(access_token)
        if user_res and user_res.user:
            session['user_id'] = user_res.user.id
            session['user_email'] = user_res.user.email
            session['access_token'] = access_token
            return jsonify({'ok': True})
    except Exception as e:
        app.logger.warning("set-session error: %s", e)
        return jsonify({'error': str(e)}), 400
    return jsonify({'error': 'invalid token'}), 400


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == os.environ.get('ADMIN_PASSWORD'):  # 環境変数で管理
            session['admin'] = True
            flash('ログイン成功')
            return redirect(url_for('admin_dashboard'))
        else:
            flash('パスワードが間違っています')
    return render_template('login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    flash('ログアウトしました')
    return redirect(url_for('index'))


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/admin/delete/<int:id>', methods=['POST'])
@admin_required
def admin_delete(id):
    try:
        resp = supabase.table("Cards").select("image_url, image_url2").eq("id", id).single().execute()
        card = resp.data or {}

        safe_unlink(local_path_from_image_url(card.get("image_url")))
        safe_unlink(local_path_from_image_url(card.get("image_url2")))

        # Supabase行の削除
        supabase.table("Cards").delete().eq("id", id).execute()

        flash('カードを削除しました')
    except Exception as e:
        flash(f'削除時にエラーが発生しました: {e}')

    return redirect(url_for('admin_dashboard'))
@app.route('/delete_image/<int:id>', methods=['POST'])
@admin_required
def delete_image(id):
    target = (request.form.get('target') or 'both').lower()

    try:
        resp = supabase.table("Cards").select("image_url, image_url2").eq("id", id).single().execute()
        row = resp.data or {}

        update_data = {}

        if target in ('1', 'both'):
            safe_unlink(local_path_from_image_url(row.get("image_url")))
            update_data["image_url"] = None
            update_data["image_public_id"] = None

        if target in ('2', 'both'):
            safe_unlink(local_path_from_image_url(row.get("image_url2")))
            update_data["image_url2"] = None
            update_data["image_public_id2"] = None

        if update_data:
            supabase.table("Cards").update(update_data).eq("id", id).execute()

        msg = "画像1と画像2を削除しました" if target == 'both' else f"画像{target}を削除しました"
        flash(msg)

    except Exception as e:
        flash(f"画像削除エラー: {e}")

    return redirect(url_for('edit_card', id=id))


# ===== デッキ機能ルート =====

# Special カードの ID マッピング
DORMAGEDDON_X_IDS = [403, 404, 405, 406, 407]
ZERON_IDS = [398, 399, 400, 401, 402]
DOKINDAM_X_ID = 270

# カードタイプで超次元ゾーン判定に使うキーワード
HYPERSPATIAL_KEYWORDS = ['dragheart', 'psychic', 'duelmate']
GACHARANGE_KEYWORD = 'gacharange'
# オリジナルで使えないカードタイプキーワード
ORIGINAL_BANNED_KEYWORDS = ['dragheart', 'psychic', 'gacharange', 'duelmate',
                            'final forbidden field', 'zeron nebula']


@app.route('/deck/new')
@login_required
def deck_new():
    """フォーマット選択ページ"""
    return render_template('deck_format_select.html')


@app.route('/deck/build')
@login_required
def deck_build():
    """デッキ作成モードでカード検索"""
    fmt = request.args.get('format', 'original')
    if fmt not in ('original', 'advanced', 'free'):
        fmt = 'original'
    # Fetch detail_types (custom card types) for the Card Type dropdown
    try:
        _resp = supabase.table("Cards").select("card_type, twin_card_type").execute()
        _all = _resp.data or []
        import re as _re
        import unicodedata as _uc
        _space_p = _re.compile(r"\s+")
        def _nct(s):
            s = _uc.normalize("NFKC", (s or "")).strip().lower()
            return _space_p.sub(" ", s)
        _PRESET = {_nct(p) for p in ["Creature","Evolution Creature","Spell","Tamaseed","Field","GR","Dragheart","Psychic","Castle","Cross Gear","Aura","Duelist"]}
        _dt_set = set()
        for c in _all:
            for k in ("card_type", "twin_card_type"):
                v = _nct(c.get(k))
                if v and v not in _PRESET:
                    _dt_set.add(v)
        detail_types = sorted(_dt_set, key=lambda x: x.lower())
    except Exception:
        detail_types = []
    is_admin = bool(session.get('admin'))
    return render_template('deck_build.html', deck_format=fmt, detail_types=detail_types, is_admin=is_admin)


@app.route('/api/cards')
def api_cards():
    """デッキ作成用: カード検索 API（JSON を返す）- full filter support"""
    query_text = request.args.get('query', '').strip()
    tribe = request.args.get('tribe', '').strip()
    search_in_raw = request.args.getlist('search_in')
    search_in = search_in_raw if search_in_raw else ['name']
    page = request.args.get('page', 1, type=int)
    per_page = 20

    # Additional filters (same as /search)
    civilizations_raw = request.args.get('civilization', '')
    selected_civs = [c.strip() for c in civilizations_raw.split(',') if c.strip()]
    color_mode = request.args.get('color_mode', 'all')
    civ_mode = request.args.get('mode', 'or')
    cost_min = request.args.get('cost_min', type=int)
    cost_max = request.args.get('cost_max', type=int)
    power_min = request.args.get('power_min', type=int)
    power_max = request.args.get('power_max', type=int)
    card_types_raw = request.args.get('card_type', '')
    selected_card_types = [x.strip() for x in card_types_raw.split(',') if x.strip()]
    card_types_detail_raw = request.args.get('card_type_detail', '')
    selected_detail_types = [x.strip() for x in card_types_detail_raw.split(',') if x.strip()]
    hof_only = request.args.get('hof_only') == '1'

    try:
        response = supabase.table("Cards").select("*").execute()
        all_cards = response.data or []

        # -- Text search --
        if query_text:
            qn = normalize_text(query_text)

            def card_blob_norm(card: dict) -> str:
                parts = []
                if 'name' in search_in:
                    parts += [card.get('name_ja'), card.get('name_ja_kana'), card.get('name_en'),
                              card.get('twin_name_ja'), card.get('twin_name_ja_kana'), card.get('twin_name_en')]
                if 'text' in search_in:
                    parts += [card.get('text_ja'), card.get('text_en'),
                              card.get('twin_text_ja'), card.get('twin_text_en'), card.get('note')]
                if 'illustrator' in search_in:
                    parts += [card.get('illustrator')]
                joined = " ".join([p for p in parts if p])
                return normalize_text(joined)

            if qn:
                all_cards = [c for c in all_cards if qn in card_blob_norm(c)]
            else:
                def card_blob_raw(card):
                    parts = [card.get('name_ja'), card.get('name_en'),
                             card.get('twin_name_ja'), card.get('twin_name_en'),
                             card.get('name_ja_kana'), card.get('twin_name_ja_kana')]
                    return normalize_keep_symbols(" ".join([p for p in parts if p]))
                qraw = normalize_keep_symbols(query_text)
                if qraw:
                    all_cards = [c for c in all_cards if qraw in card_blob_raw(c)]

        # -- Tribe --
        if tribe:
            all_cards = [c for c in all_cards if tribe.lower() in (c.get('tribe') or '').lower()]

        # -- HoF only --
        if hof_only:
            all_cards = [c for c in all_cards if to_int(c.get('regulation_type')) not in (None, 0)]

        # -- Civilization helpers (same as search()) --
        def _is_multi(s): return '/' in s if s else False
        def _is_mono(s): return (s or '') != '' and '/' not in s
        def _civ_tokens(s):
            if not s: return []
            s = s.replace('／', '/')
            return [p.strip() for p in s.split('/') if p.strip()]
        def _field_matches(fv, target, cm):
            if not fv: return False
            if cm == 'mono' and not _is_mono(fv): return False
            if cm == 'multi' and not _is_multi(fv): return False
            tokens = _civ_tokens(fv)
            t = target.lower()
            return any(tok.lower() == t for tok in tokens) if tokens else (t in fv.lower())
        def _card_matches_civ(card, target, cm):
            return (_field_matches(card.get('civilization'), target, cm) or
                    _field_matches(card.get('twin_civilization'), target, cm))

        if selected_civs:
            if civ_mode == 'and':
                all_cards = [c for c in all_cards if all(_card_matches_civ(c, civ, color_mode) for civ in selected_civs)]
            else:
                all_cards = [c for c in all_cards if any(_card_matches_civ(c, civ, color_mode) for civ in selected_civs)]
        elif color_mode in ('mono', 'multi'):
            def _card_is_mono(card):
                main = card.get('civilization') or ''; twin = card.get('twin_civilization') or ''
                if _is_multi(main) or _is_multi(twin): return False
                return bool(main or twin)
            def _card_is_multi(card):
                return _is_multi(card.get('civilization') or '') or _is_multi(card.get('twin_civilization') or '')
            if color_mode == 'mono':
                all_cards = [c for c in all_cards if _card_is_mono(c)]
            else:
                all_cards = [c for c in all_cards if _card_is_multi(c)]

        # -- Card type --
        _space = re.compile(r"\s+")
        def _norm_ct(s):
            s = unicodedata.normalize("NFKC", (s or "")).strip().lower()
            return _space.sub(" ", s)
        PRESET_NORMS_API = {_norm_ct(p) for p in ["Creature","Evolution Creature","Spell","Tamaseed","Field","GR","Dragheart","Psychic","Castle","Cross Gear","Aura","Duelist"]}
        def _is_exact_preset(ct): return _norm_ct(ct) in PRESET_NORMS_API
        def _ct_match_preset(card, token):
            main = _norm_ct(card.get("card_type")); twin = _norm_ct(card.get("twin_card_type"))
            def af(pred): return pred(main) or pred(twin)
            if token == "Creature_all": return af(lambda s: "creature" in s)
            if token == "Creature_only": return af(lambda s: s == "creature")
            if token == "other": return af(lambda s: s != "" and not _is_exact_preset(s))
            t = token.lower(); return af(lambda s: t in s)
        def _ct_match_detail(card, detail):
            d = _norm_ct(detail)
            return _norm_ct(card.get("card_type")) == d or _norm_ct(card.get("twin_card_type")) == d

        if selected_card_types or selected_detail_types:
            all_cards = [c for c in all_cards if
                         any(_ct_match_preset(c, t) for t in selected_card_types) or
                         any(_ct_match_detail(c, d) for d in selected_detail_types)]

        # -- Cost range --
        if cost_min is not None or cost_max is not None:
            def _in_cost(v):
                if v is None: return False
                if cost_min is not None and v < cost_min: return False
                if cost_max is not None and v > cost_max: return False
                return True
            all_cards = [c for c in all_cards if _in_cost(to_int(c.get('cost'))) or _in_cost(to_int(c.get('twin_cost')))]

        # -- Power range --
        if power_min is not None:
            all_cards = [c for c in all_cards if
                         (to_int(c.get('power')) is not None and to_int(c.get('power')) >= power_min) or
                         (to_int(c.get('twin_power')) is not None and to_int(c.get('twin_power')) >= power_min)]
        if power_max is not None:
            all_cards = [c for c in all_cards if
                         (to_int(c.get('power')) is not None and to_int(c.get('power')) <= power_max) or
                         (to_int(c.get('twin_power')) is not None and to_int(c.get('twin_power')) <= power_max)]

        all_cards.sort(key=lambda c: c.get("id", 0), reverse=True)

        total = len(all_cards)
        start = (page - 1) * per_page
        paginated = all_cards[start:start + per_page]

        for c in paginated:
            c["image_url"] = normalize_image_url_for_env(c.get("image_url"))
            c["image_url2"] = normalize_image_url_for_env(c.get("image_url2"))
            c["regulation_label"] = get_regulation_label(c.get("regulation_type"))

        # Attach card group info to cards
        paginated_ids = [c["id"] for c in paginated]
        group_map = get_card_group_map(paginated_ids)
        for c in paginated:
            g = group_map.get(c["id"])
            if g:
                c["group_id"] = g["group_id"]
                c["group_members"] = g["members"]
                c["group_rep_image_url"] = g["rep_image_url"]
            else:
                c["group_id"] = None
                c["group_members"] = []

        pages = (total + per_page - 1) // per_page
        return jsonify({"cards": paginated, "page": page, "pages": pages, "total": total})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ========== Card Group APIs ==========

@app.route('/api/card_groups')
@admin_required
def api_card_groups_list():
    """List all card groups with their members."""
    try:
        groups_res = supabase.table("card_groups").select("*").order("created_at", desc=True).execute()
        groups = groups_res.data or []
        if not groups:
            return jsonify({"groups": []})
        group_ids = [g["id"] for g in groups]
        members_res = supabase.table("card_group_members").select("group_id, card_id, position").in_("group_id", group_ids).order("position").execute()
        members = members_res.data or []
        # Attach card info
        card_ids = list(set(m["card_id"] for m in members))
        cards_map = {}
        if card_ids:
            c_res = supabase.table("Cards").select("id, name_en, name_ja, image_url").in_("id", card_ids).execute()
            for c in (c_res.data or []):
                c["image_url"] = normalize_image_url_for_env(c.get("image_url"))
                cards_map[c["id"]] = c
        # Build groups with members
        members_by_group = {}
        for m in members:
            gid = m["group_id"]
            if gid not in members_by_group:
                members_by_group[gid] = []
            card_info = cards_map.get(m["card_id"], {})
            members_by_group[gid].append({
                "card_id": m["card_id"],
                "position": m["position"],
                "name": card_info.get("name_en") or card_info.get("name_ja") or str(m["card_id"]),
                "image_url": card_info.get("image_url") or "",
            })
        for g in groups:
            g["members"] = members_by_group.get(g["id"], [])
        return jsonify({"groups": groups})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/card_groups', methods=['POST'])
@admin_required
def api_card_groups_create():
    """Create a new card group."""
    data = request.json or {}
    card_ids = data.get("card_ids", [])
    name = data.get("name", "")
    if len(card_ids) < 2:
        return jsonify({"error": "At least 2 cards required"}), 400
    try:
        g_res = supabase.table("card_groups").insert({"name": name}).execute()
        group_id = g_res.data[0]["id"]
        rows = [{"group_id": group_id, "card_id": int(cid), "position": i} for i, cid in enumerate(card_ids)]
        supabase.table("card_group_members").insert(rows).execute()
        return jsonify({"ok": True, "group_id": group_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/card_groups/<int:group_id>', methods=['DELETE'])
@admin_required
def api_card_groups_delete(group_id):
    """Delete a card group."""
    try:
        supabase.table("card_group_members").delete().eq("group_id", group_id).execute()
        supabase.table("card_groups").delete().eq("id", group_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def get_card_group_map(card_ids):
    """For a list of card_ids, return a dict mapping card_id -> group info."""
    if not card_ids:
        return {}
    try:
        m_res = supabase.table("card_group_members").select("group_id, card_id, position").in_("card_id", [int(c) for c in card_ids]).execute()
        members = m_res.data or []
        if not members:
            return {}
        group_ids = list(set(m["group_id"] for m in members))
        all_members_res = supabase.table("card_group_members").select("group_id, card_id, position").in_("group_id", group_ids).order("position").execute()
        all_members = all_members_res.data or []
        all_card_ids = list(set(m["card_id"] for m in all_members))
        cards_res = supabase.table("Cards").select("id, name_en, name_ja, image_url, image_url2").in_("id", all_card_ids).execute()
        cards_map = {}
        for c in (cards_res.data or []):
            c["image_url"] = normalize_image_url_for_env(c.get("image_url"))
            c["image_url2"] = normalize_image_url_for_env(c.get("image_url2"))
            cards_map[c["id"]] = c
        members_by_group = {}
        for m in all_members:
            gid = m["group_id"]
            if gid not in members_by_group:
                members_by_group[gid] = []
            card_info = cards_map.get(m["card_id"], {})
            members_by_group[gid].append({
                "card_id": m["card_id"],
                "position": m["position"],
                "name": card_info.get("name_en") or card_info.get("name_ja") or str(m["card_id"]),
                "image_url": card_info.get("image_url") or "",
                "image_url2": card_info.get("image_url2") or None,
            })
        # Build result: card_id -> {group_id, members, rep_image}
        result = {}
        for m in members:
            cid = m["card_id"]
            gid = m["group_id"]
            group_members = members_by_group.get(gid, [])
            rep = group_members[0] if group_members else {}
            result[cid] = {
                "group_id": gid,
                "rep_image_url": rep.get("image_url") or "",
                "rep_name": rep.get("name") or "",
                "members": group_members,
            }
        return result
    except Exception:
        return {}


@app.route('/api/admin/upload_cover', methods=['POST'])
@admin_required
def api_admin_upload_cover():
    """管理者用 デッキカバー画像アップロード API"""
    f = request.files.get('image')
    if not f or not f.filename:
        return jsonify({'error': 'No file provided'}), 400
    if not allowed_file(f.filename):
        return jsonify({'error': 'Invalid file type. Use JPG/PNG.'}), 400
    # Force .jpg extension when saving
    ext = f.filename.rsplit('.', 1)[1].lower()
    if ext not in ('jpg', 'jpeg', 'png'):
        return jsonify({'error': 'Only JPG or PNG files are accepted.'}), 400
    save_ext = 'jpg' if ext in ('jpg', 'jpeg') else 'png'
    filename = 'cover_' + uuid.uuid4().hex + '.' + save_ext
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    f.save(save_path)
    url = '/uploads/' + filename
    return jsonify({'ok': True, 'url': url})


@app.route('/api/deck/save', methods=['POST'])
@login_required
def api_deck_save():
    """デッキ保存 API"""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'invalid data'}), 400

    # Support admin session
    is_admin = bool(session.get('admin'))
    user_id = ADMIN_USER_ID if is_admin else session.get('user_id')
    if not user_id:
        return jsonify({'error': 'not authenticated'}), 401

    deck_id = data.get('deck_id')  # 編集時は既存ID
    name = (data.get('name') or 'Deck').strip()
    fmt = data.get('format', 'original')
    description = data.get('description', '')
    cover_card_id = data.get('cover_card_id')
    cover_image_url = (data.get('cover_image_url') or '').strip() or None  # admin external image
    special_type = data.get('special_type')  # null / 'dormageddon_x' / 'zeron'
    is_public = bool(data.get('is_public', False))
    cards = data.get('cards', [])  # [{card_id, zone, quantity}, ...]

    if fmt not in ('original', 'advanced', 'free'):
        return jsonify({'error': 'invalid format'}), 400
    if not name:
        return jsonify({'error': 'deck name is required'}), 400
    if special_type and special_type not in ('dormageddon_x', 'zeron'):
        return jsonify({'error': 'invalid special_type'}), 400

    try:
        if deck_id:
            # 既存デッキの更新
            update_data = {
                "name": name,
                "format": fmt,
                "description": description,
                "cover_card_id": cover_card_id,
                "special_type": special_type,
                "is_public": is_public,
                "updated_at": "now()",
            }
            if cover_image_url is not None:
                update_data["cover_image_url"] = cover_image_url
            supabase.table("decks").update(update_data).eq("id", deck_id).eq("user_id", user_id).execute()

            # 既存カードを削除して再挿入
            supabase.table("deck_cards").delete().eq("deck_id", deck_id).execute()
        else:
            # 新規作成
            insert_data = {
                "user_id": user_id,
                "name": name,
                "format": fmt,
                "description": description,
                "cover_card_id": cover_card_id,
                "special_type": special_type,
                "is_public": is_public,
            }
            if cover_image_url is not None:
                insert_data["cover_image_url"] = cover_image_url
            res = supabase.table("decks").insert(insert_data).execute()
            deck_id = res.data[0]["id"]

        # カードを挿入
        if cards:
            rows = []
            for c in cards:
                row = {"deck_id": deck_id, "card_id": c["card_id"],
                       "zone": c["zone"], "quantity": c["quantity"]}
                if c.get("group_id"):
                    row["group_id"] = int(c["group_id"])
                rows.append(row)
            supabase.table("deck_cards").insert(rows).execute()

        return jsonify({"ok": True, "deck_id": deck_id})
    except Exception as e:
        app.logger.warning("Deck save error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route('/print')
def print_page():
    """カード印刷ページ"""
    return render_template('print.html')


@app.route('/print/search')
def print_search():
    """印刷用カード検索ページ"""
    try:
        _resp = supabase.table("Cards").select("card_type, twin_card_type").execute()
        _all = _resp.data or []
        import re as _re
        import unicodedata as _uc
        _sp = _re.compile(r"\s+")
        def _nct(s):
            s = _uc.normalize("NFKC", (s or "")).strip().lower()
            return _sp.sub(" ", s)
        _PRESET = {_nct(p) for p in ["Creature","Evolution Creature","Spell","Tamaseed","Field","GR","Dragheart","Psychic","Castle","Cross Gear","Aura","Duelist"]}
        _dt = set()
        for c in _all:
            for k in ("card_type", "twin_card_type"):
                v = _nct(c.get(k))
                if v and v not in _PRESET:
                    _dt.add(v)
        detail_types = sorted(_dt, key=lambda x: x.lower())
    except Exception:
        detail_types = []
    return render_template('print_search.html', detail_types=detail_types)


@app.route('/decks')
def deck_list():
    """デッキ閲覧ページ"""
    return render_template('deck_list.html')


@app.route('/api/decks')
def api_deck_list():
    """デッキ一覧 API"""
    mode = request.args.get('mode', 'public')  # 'public' or 'my'
    page = request.args.get('page', 1, type=int)
    per_page = 20
    keyword = request.args.get('keyword', '').strip()
    fmt_filter = request.args.get('format', '').strip()
    card_keyword = request.args.get('card_keyword', '').strip()
    date_from_str = request.args.get('date_from', '').strip()
    date_to_str = request.args.get('date_to', '').strip()
    civ_filter_str = request.args.get('civilizations', '').strip()
    sort_by = request.args.get('sort_by', 'newest')  # 'newest' or 'likes'
    filter_liked = request.args.get('filter_liked', '0') == '1'
    filter_bookmarked = request.args.get('filter_bookmarked', '0') == '1'
    # Search target flags (default: name and description checked)
    st_name = request.args.get('st_name', '1') != '0'
    st_desc = request.args.get('st_desc', '1') != '0'
    st_card = request.args.get('st_card', '0') == '1'
    st_username = request.args.get('st_username', '0') == '1'

    # Parse civilization filter list
    civ_filter = [c.strip() for c in civ_filter_str.split(',') if c.strip()] if civ_filter_str else []

    # Parse date range (YYYY/MM/DD → ISO string prefix)
    def parse_date(s):
        """Convert YYYY/MM/DD to YYYY-MM-DD or return None"""
        if not s:
            return None
        import re as _re
        m = _re.match(r'^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})$', s)
        if m:
            return '{}-{:02d}-{:02d}'.format(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return None

    date_from = parse_date(date_from_str)
    date_to = parse_date(date_to_str)

    # Canonical civilization names for detection
    CANONICAL_CIVS = ['Fire', 'Water', 'Nature', 'Light', 'Darkness', 'Colorless']

    def detect_civs_from_str(civ_str):
        """Detect canonical civilizations from a civilization string."""
        if not civ_str:
            return []
        s = civ_str.lower()
        found = []
        for civ in CANONICAL_CIVS:
            if civ.lower() in s:
                found.append(civ)
        return found

    def get_deck_civs(deck_cards_data, cards_data_map):
        """Build deduplicated list of canonical civs for a deck."""
        seen = set()
        result = []
        for dc in deck_cards_data:
            card = cards_data_map.get(dc.get('card_id'))
            if not card:
                continue
            for field in ['civilization', 'twin_civilization']:
                civ_str = card.get(field) or ''
                for civ in detect_civs_from_str(civ_str):
                    if civ not in seen:
                        seen.add(civ)
                        result.append(civ)
        return result

    try:
        if mode == 'my':
            user_id = session.get('user_id')
            if not user_id:
                return jsonify({"decks": [], "page": 1, "pages": 0, "total": 0})
            query = supabase.table("decks").select("*").eq("user_id", user_id)
        else:
            query = supabase.table("decks").select("*").eq("is_public", True)

        query = query.order("created_at", desc=True)
        response = query.execute()
        all_decks = response.data or []

        # Format filter
        if fmt_filter:
            all_decks = [d for d in all_decks if d.get('format') == fmt_filter]

        # Date range filter (compare ISO date strings)
        if date_from:
            all_decks = [d for d in all_decks
                         if d.get('created_at') and d['created_at'][:10] >= date_from]
        if date_to:
            all_decks = [d for d in all_decks
                         if d.get('created_at') and d['created_at'][:10] <= date_to]

        # Keyword filter (deck name / description / username)
        # For username search, we need to fetch profiles first
        username_map = {}  # user_id -> username (for all decks' owners)
        if keyword and st_username:
            owner_ids = list(set(d.get('user_id') for d in all_decks if d.get('user_id')))
            username_map = _get_usernames_bulk(owner_ids)
            # Fill default: email for users without a profile
            # (we don't have email here, so blank stays blank - handled below)

        if keyword and (st_name or st_desc or st_username):
            kw = keyword.lower()
            def deck_matches_kw(d):
                if st_name and kw in (d.get('name') or '').lower():
                    return True
                if st_desc and kw in (d.get('description') or '').lower():
                    return True
                if st_username:
                    uname = username_map.get(d.get('user_id'), '').lower()
                    if kw in uname:
                        return True
                return False
            all_decks = [d for d in all_decks if deck_matches_kw(d)]

        # Card keyword filter: requires fetching deck cards + card names
        # and/or civilization filter: also requires card data
        # Do this in one pass if either is active
        needs_card_data = bool(card_keyword and st_card) or bool(civ_filter)

        # Build a set of deck IDs that pass card-level filters
        passing_deck_ids = None  # None means "all pass"
        deck_civs_map = {}  # deck_id -> list of canonical civs

        if needs_card_data:
            deck_ids = [d['id'] for d in all_decks]
            if deck_ids:
                # Fetch all deck_cards for these decks
                dc_res = supabase.table("deck_cards").select("deck_id, card_id").in_("deck_id", deck_ids).execute()
                all_dc = dc_res.data or []

                # Build map: deck_id -> [card_ids]
                deck_to_card_ids = {}
                for dc in all_dc:
                    did = dc['deck_id']
                    deck_to_card_ids.setdefault(did, [])
                    deck_to_card_ids[did].append(dc['card_id'])

                # Fetch all card data needed
                all_card_ids = list(set(dc['card_id'] for dc in all_dc))
                cards_data_map = {}
                if all_card_ids:
                    card_fields = "id, name_en, twin_name_en, name_ja, name_ja_kana, twin_name_ja, twin_name_ja_kana, civilization, twin_civilization"
                    cr = supabase.table("Cards").select(card_fields).in_("id", all_card_ids).execute()
                    for c in (cr.data or []):
                        cards_data_map[c['id']] = c

                # Build deck-level civilizations and apply filters
                passing_deck_ids = set()
                for deck in all_decks:
                    did = deck['id']
                    dc_list = [{'card_id': cid} for cid in deck_to_card_ids.get(did, [])]
                    civs = get_deck_civs(dc_list, cards_data_map)
                    deck_civs_map[did] = civs

                    # Civilization filter: deck must contain ALL selected civs
                    if civ_filter:
                        if not all(c in civs for c in civ_filter):
                            continue

                    # Card keyword filter
                    if card_keyword and st_card:
                        ckw = card_keyword.lower()
                        card_ids_in_deck = deck_to_card_ids.get(did, [])
                        found = False
                        for cid in card_ids_in_deck:
                            card = cards_data_map.get(cid)
                            if not card:
                                continue
                            name_fields = [
                                card.get('name_en'), card.get('twin_name_en'),
                                card.get('name_ja'), card.get('name_ja_kana'),
                                card.get('twin_name_ja'), card.get('twin_name_ja_kana'),
                            ]
                            if any(ckw in (f or '').lower() for f in name_fields):
                                found = True
                                break
                        if not found:
                            continue

                    passing_deck_ids.add(did)

                if passing_deck_ids is not None:
                    all_decks = [d for d in all_decks if d['id'] in passing_deck_ids]

        # Like/bookmark filters (only for logged-in users)
        current_user_id = session.get('user_id')
        user_liked_ids = set()
        user_bookmarked_ids = set()
        if current_user_id and all_decks:
            all_deck_ids = [d['id'] for d in all_decks]
            if filter_liked or filter_bookmarked or True:  # always fetch for badges
                ul_res = supabase.table("deck_likes").select("deck_id").eq("user_id", current_user_id).in_("deck_id", all_deck_ids).execute()
                user_liked_ids = {r['deck_id'] for r in (ul_res.data or [])}
                ub_res = supabase.table("deck_bookmarks").select("deck_id").eq("user_id", current_user_id).in_("deck_id", all_deck_ids).execute()
                user_bookmarked_ids = {r['deck_id'] for r in (ub_res.data or [])}

        if filter_liked and current_user_id:
            all_decks = [d for d in all_decks if d['id'] in user_liked_ids]
        if filter_bookmarked and current_user_id:
            all_decks = [d for d in all_decks if d['id'] in user_bookmarked_ids]

        # Fetch like counts for all remaining decks
        like_counts = {}
        if all_decks:
            remaining_ids = [d['id'] for d in all_decks]
            lc_res = supabase.table("deck_likes").select("deck_id").in_("deck_id", remaining_ids).execute()
            for r in (lc_res.data or []):
                like_counts[r['deck_id']] = like_counts.get(r['deck_id'], 0) + 1

        # Sort
        if sort_by == 'likes':
            all_decks.sort(key=lambda d: like_counts.get(d['id'], 0), reverse=True)
        # else keep created_at desc (already ordered from DB)

        total = len(all_decks)
        start = (page - 1) * per_page
        paginated = all_decks[start:start + per_page]

        # Cover card image_url
        cover_ids = [d['cover_card_id'] for d in paginated if d.get('cover_card_id')]
        cover_map = {}
        if cover_ids:
            cres = supabase.table("Cards").select("id, image_url").in_("id", cover_ids).execute()
            for c in (cres.data or []):
                cover_map[c['id']] = normalize_image_url_for_env(c.get('image_url'))

        # For decks not yet in deck_civs_map (when needs_card_data was false),
        # compute civs now for the paginated set
        if not needs_card_data:
            paginated_ids = [d['id'] for d in paginated]
            if paginated_ids:
                dc_res = supabase.table("deck_cards").select("deck_id, card_id").in_("deck_id", paginated_ids).execute()
                all_dc = dc_res.data or []
                all_card_ids = list(set(dc['card_id'] for dc in all_dc))
                cards_data_map = {}
                if all_card_ids:
                    cr = supabase.table("Cards").select("id, civilization, twin_civilization").in_("id", all_card_ids).execute()
                    for c in (cr.data or []):
                        cards_data_map[c['id']] = c
                deck_to_card_ids = {}
                for dc in all_dc:
                    deck_to_card_ids.setdefault(dc['deck_id'], [])
                    deck_to_card_ids[dc['deck_id']].append(dc['card_id'])
                for deck in paginated:
                    did = deck['id']
                    dc_list = [{'card_id': cid} for cid in deck_to_card_ids.get(did, [])]
                    deck_civs_map[did] = get_deck_civs(dc_list, cards_data_map)

        # Fetch usernames for deck owners
        paginated_owner_ids = list(set(d.get('user_id') for d in paginated if d.get('user_id')))
        # Merge with already-fetched username_map (from username search above)
        if paginated_owner_ids:
            extra_map = _get_usernames_bulk(
                [uid for uid in paginated_owner_ids if uid not in username_map]
            )
            username_map.update(extra_map)

        for d in paginated:
            # Cover image: use deck's own cover_image_url if set (admin external), else card image
            deck_cover_img = d.get('cover_image_url') or None
            if not deck_cover_img:
                deck_cover_img = cover_map.get(d.get('cover_card_id'))
            d['cover_image_url'] = deck_cover_img
            d['civilizations'] = deck_civs_map.get(d['id'], [])
            deck_owner_id = d.get('user_id')
            if deck_owner_id == ADMIN_USER_ID:
                d['creator_username'] = ADMIN_DISPLAY_NAME
                d['is_official'] = True
            else:
                owner_uname = username_map.get(deck_owner_id, '')
                d['creator_username'] = owner_uname if owner_uname else (deck_owner_id or 'Unknown')
                d['is_official'] = False
            d['like_count'] = like_counts.get(d['id'], 0)
            d['is_liked'] = d['id'] in user_liked_ids
            d['is_bookmarked'] = d['id'] in user_bookmarked_ids

        pages = (total + per_page - 1) // per_page
        return jsonify({"decks": paginated, "page": page, "pages": pages, "total": total})
    except Exception as e:
        app.logger.warning("api_deck_list error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route('/api/deck/<int:deck_id>/like', methods=['POST'])
def api_deck_like(deck_id):
    """いいねトグル API"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"error": "Login required"}), 401
    try:
        existing = supabase.table("deck_likes").select("id").eq("user_id", user_id).eq("deck_id", deck_id).execute()
        if existing.data:
            supabase.table("deck_likes").delete().eq("user_id", user_id).eq("deck_id", deck_id).execute()
            liked = False
        else:
            supabase.table("deck_likes").insert({"user_id": user_id, "deck_id": deck_id}).execute()
            liked = True
        # Count current likes
        cnt = supabase.table("deck_likes").select("id", count="exact").eq("deck_id", deck_id).execute()
        like_count = cnt.count if hasattr(cnt, 'count') and cnt.count is not None else len(cnt.data or [])
        return jsonify({"ok": True, "liked": liked, "like_count": like_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/deck/<int:deck_id>/bookmark', methods=['POST'])
def api_deck_bookmark(deck_id):
    """ブックマークトグル API"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"error": "Login required"}), 401
    try:
        existing = supabase.table("deck_bookmarks").select("id").eq("user_id", user_id).eq("deck_id", deck_id).execute()
        if existing.data:
            supabase.table("deck_bookmarks").delete().eq("user_id", user_id).eq("deck_id", deck_id).execute()
            bookmarked = False
        else:
            supabase.table("deck_bookmarks").insert({"user_id": user_id, "deck_id": deck_id}).execute()
            bookmarked = True
        return jsonify({"ok": True, "bookmarked": bookmarked})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/deck/<int:deck_id>')
def deck_detail(deck_id):
    """デッキ詳細表示ページ"""
    try:
        # デッキ情報
        dres = supabase.table("decks").select("*").eq("id", deck_id).single().execute()
        deck = dres.data
        if not deck:
            abort(404)

        # 公開チェック
        user_id = session.get('user_id')
        is_admin = bool(session.get('admin'))
        # Admin can view all decks; owners can view their own private decks
        if not deck.get('is_public') and deck.get('user_id') != user_id:
            if not (is_admin and deck.get('user_id') == ADMIN_USER_ID):
                abort(403)

        # デッキ内のカード
        cres = supabase.table("deck_cards").select("*").eq("deck_id", deck_id).execute()
        deck_cards = cres.data or []

        # カード情報を取得
        card_ids = list(set([dc['card_id'] for dc in deck_cards]))
        cards_map = {}
        if card_ids:
            cards_res = supabase.table("Cards").select("*").in_("id", card_ids).execute()
            for c in (cards_res.data or []):
                c["image_url"] = normalize_image_url_for_env(c.get("image_url"))
                c["image_url2"] = normalize_image_url_for_env(c.get("image_url2"))
                cards_map[c["id"]] = c

        # Fetch group info for any group deck_cards
        group_ids_in_deck = list(set(dc["group_id"] for dc in deck_cards if dc.get("group_id")))
        group_detail_map = {}  # group_id -> {members: [...]}
        if group_ids_in_deck:
            gm_res = supabase.table("card_group_members").select("group_id, card_id, position").in_("group_id", group_ids_in_deck).order("position").execute()
            gm_members = gm_res.data or []
            all_group_card_ids = list(set(m["card_id"] for m in gm_members))
            # Fetch any cards not already in cards_map
            missing_ids = [cid for cid in all_group_card_ids if cid not in cards_map]
            if missing_ids:
                extra_res = supabase.table("Cards").select("*").in_("id", missing_ids).execute()
                for c in (extra_res.data or []):
                    c["image_url"] = normalize_image_url_for_env(c.get("image_url"))
                    c["image_url2"] = normalize_image_url_for_env(c.get("image_url2"))
                    cards_map[c["id"]] = c
            for m in gm_members:
                gid = m["group_id"]
                if gid not in group_detail_map:
                    group_detail_map[gid] = {"members": []}
                card_info = cards_map.get(m["card_id"], {})
                group_detail_map[gid]["members"].append({
                    "card_id": m["card_id"],
                    "position": m["position"],
                    "image_url": card_info.get("image_url") or "",
                    "image_url2": card_info.get("image_url2") or None,
                    "name": card_info.get("name_en") or card_info.get("name_ja") or str(m["card_id"]),
                })

        is_owner = (user_id is not None and deck.get('user_id') == user_id) or \
                   (is_admin and deck.get('user_id') == ADMIN_USER_ID)

        # Fetch creator username
        creator_user_id = deck.get('user_id')
        if creator_user_id == ADMIN_USER_ID:
            creator_username = ADMIN_DISPLAY_NAME
            is_official = True
        else:
            creator_username = _get_username(creator_user_id) if creator_user_id else ''
            if not creator_username and creator_user_id:
                creator_username = creator_user_id  # fallback
            is_official = False

        # Build print list data for the Print Deck button
        # For group cards, expand to all group member images
        deck_print_json = []
        for dc in deck_cards:
            gid = dc.get("group_id")
            if gid and gid in group_detail_map:
                # Group card: print all members (qty times)
                for member in group_detail_map[gid]["members"]:
                    deck_print_json.append({
                        'card_id': member['card_id'],
                        'name': member['name'],
                        'image_url': member['image_url'],
                        'image_url2': member.get('image_url2') or None,
                        'qty': dc.get('quantity', 1)
                    })
            else:
                card = cards_map.get(dc['card_id'])
                if card:
                    deck_print_json.append({
                        'card_id': card['id'],
                        'name': card.get('name_en') or card.get('name_ja') or 'Unknown',
                        'image_url': card.get('image_url') or '',
                        'image_url2': card.get('image_url2') or None,
                        'qty': dc.get('quantity', 1)
                    })

        return render_template('deck_detail.html', deck=deck, deck_cards=deck_cards,
                               cards_map=cards_map, is_owner=is_owner,
                               creator_username=creator_username,
                               is_official=is_official,
                               deck_print_json=deck_print_json,
                               group_detail_map=group_detail_map)
    except Exception as e:
        return f"Deck error: {e}", 500


@app.route('/api/deck/<int:deck_id>/load')
@login_required
def api_deck_load(deck_id):
    """デッキ編集用: デッキデータをセッションストレージ形式で返す"""
    is_admin = bool(session.get('admin'))
    user_id = ADMIN_USER_ID if is_admin else session.get('user_id')
    if not user_id:
        return jsonify({'error': 'not authenticated'}), 401
    try:
        dres = supabase.table("decks").select("*").eq("id", deck_id).eq("user_id", user_id).execute()
        if not dres.data:
            return jsonify({'error': 'Not found or not authorized'}), 404
        deck = dres.data[0]

        cres = supabase.table("deck_cards").select("*").eq("deck_id", deck_id).execute()
        deck_cards = cres.data or []

        card_ids = [dc['card_id'] for dc in deck_cards]
        cards_map = {}
        if card_ids:
            cr = supabase.table("Cards").select(
                "id, name_en, name_ja, image_url, card_type, twin_card_type, regulation_type, civilization"
            ).in_("id", card_ids).execute()
            for c in (cr.data or []):
                c['image_url'] = normalize_image_url_for_env(c.get('image_url'))
                cards_map[c['id']] = c

        # Build deck.cards dict (same format as sessionStorage state)
        # Fetch group info for any group deck_cards
        load_group_ids = list(set(dc['group_id'] for dc in deck_cards if dc.get('group_id')))
        load_group_detail = {}
        if load_group_ids:
            lgm_res = supabase.table("card_group_members").select("group_id, card_id, position").in_("group_id", load_group_ids).order("position").execute()
            lgm_members = lgm_res.data or []
            all_lg_cids = list(set(m['card_id'] for m in lgm_members))
            missing = [cid for cid in all_lg_cids if cid not in cards_map]
            if missing:
                ex_res = supabase.table("Cards").select("id, name_en, name_ja, image_url, image_url2").in_("id", missing).execute()
                for c in (ex_res.data or []):
                    c['image_url'] = normalize_image_url_for_env(c.get('image_url'))
                    cards_map[c['id']] = c
            for m in lgm_members:
                gid = m['group_id']
                if gid not in load_group_detail:
                    load_group_detail[gid] = []
                ci = cards_map.get(m['card_id'], {})
                load_group_detail[gid].append({
                    'card_id': m['card_id'],
                    'position': m['position'],
                    'name': ci.get('name_en') or ci.get('name_ja') or str(m['card_id']),
                    'image_url': ci.get('image_url') or '',
                })

        cards_dict = {}
        for dc in deck_cards:
            if dc['zone'] == 'special':
                continue  # special zone handled via deck.special
            gid = dc.get('group_id')
            if gid and gid in load_group_detail:
                members = load_group_detail[gid]
                rep = members[0] if members else {}
                gkey = 'G' + str(gid)
                cards_dict[gkey] = {
                    'zone': dc['zone'],
                    'qty': dc['quantity'],
                    'is_group': True,
                    'group_id': gid,
                    'group_members': members,
                    'name_en': rep.get('name') or 'Group',
                    'image_url': rep.get('image_url') or '',
                    'card_type': '',
                    'regulation_type': 0,
                    'civilization': '',
                }
            else:
                card = cards_map.get(dc['card_id'])
                if not card:
                    continue
                cards_dict[str(dc['card_id'])] = {
                    'zone': dc['zone'],
                    'qty': dc['quantity'],
                    'name_en': card.get('name_en') or card.get('name_ja') or 'Unknown',
                    'image_url': card.get('image_url') or '',
                    'card_type': card.get('card_type') or '',
                    'regulation_type': card.get('regulation_type') or 0,
                    'civilization': card.get('civilization') or '',
                }

        deck_state = {
            'cards': cards_dict,
            'special': deck.get('special_type'),
            'deckId': deck_id,
            # Pre-fill save modal fields when editing
            'name': deck.get('name', 'Deck'),
            'description': deck.get('description', ''),
            'is_public': deck.get('is_public', False),
            'cover_card_id': deck.get('cover_card_id'),
            'cover_image_url': deck.get('cover_image_url') or '',
        }

        return jsonify({'ok': True, 'deck_state': deck_state, 'format': deck.get('format')})
    except Exception as e:
        app.logger.warning("api_deck_load error: %s", e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/deck/<int:deck_id>/delete', methods=['POST'])
@login_required
def api_deck_delete(deck_id):
    """デッキ削除 API"""
    is_admin = bool(session.get('admin'))
    user_id = ADMIN_USER_ID if is_admin else session.get('user_id')
    if not user_id:
        return jsonify({'error': 'not authenticated'}), 401
    try:
        # 所有権確認
        dres = supabase.table("decks").select("id").eq("id", deck_id).eq("user_id", user_id).execute()
        if not dres.data:
            return jsonify({'error': 'Not found or not authorized'}), 404
        # deck_cards は ON DELETE CASCADE で自動削除される
        supabase.table("decks").delete().eq("id", deck_id).eq("user_id", user_id).execute()
        return jsonify({'ok': True})
    except Exception as e:
        app.logger.warning("api_deck_delete error: %s", e)
        return jsonify({'error': str(e)}), 500


# ===== Profile API =====

def _ensure_profile(user_id, email):
    """Ensure a profile row exists for the user; create if missing."""
    try:
        res = supabase.table("profiles").select("user_id").eq("user_id", user_id).execute()
        if not res.data:
            supabase.table("profiles").insert({"user_id": user_id, "username": email or ""}).execute()
    except Exception as e:
        app.logger.warning("_ensure_profile error: %s", e)


def _get_username(user_id):
    """Return the username for a given user_id, or empty string."""
    if not user_id:
        return ""
    try:
        res = supabase.table("profiles").select("username").eq("user_id", user_id).execute()
        if res.data:
            return res.data[0].get("username") or ""
    except Exception:
        pass
    return ""


def _get_usernames_bulk(user_ids):
    """Return {user_id: username} for a list of user_ids."""
    if not user_ids:
        return {}
    try:
        res = supabase.table("profiles").select("user_id, username").in_("user_id", list(user_ids)).execute()
        return {r["user_id"]: (r.get("username") or "") for r in (res.data or [])}
    except Exception:
        return {}


@app.route('/api/profile')
@login_required
def api_profile_get():
    """Get current user's profile (username)."""
    user_id = session.get('user_id')
    email = session.get('user_email', '')
    _ensure_profile(user_id, email)
    username = _get_username(user_id)
    # If username is blank, default to email
    if not username:
        username = email
    return jsonify({'ok': True, 'username': username, 'email': email})


@app.route('/api/profile/update', methods=['POST'])
@login_required
def api_profile_update():
    """Update current user's username."""
    user_id = session.get('user_id')
    email = session.get('user_email', '')
    data = request.get_json(force=True) or {}
    username = (data.get('username') or '').strip()
    if not username:
        return jsonify({'error': 'Username cannot be empty'}), 400
    try:
        _ensure_profile(user_id, email)
        supabase.table("profiles").update({"username": username, "updated_at": "now()"}).eq("user_id", user_id).execute()
        return jsonify({'ok': True, 'username': username})
    except Exception as e:
        app.logger.warning("api_profile_update error: %s", e)
        return jsonify({'error': str(e)}), 500








