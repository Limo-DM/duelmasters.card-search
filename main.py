# ↓ これ残す
import os
import uuid
import logging
import markdown
import re
import unicodedata
from flask import Flask, request, render_template, redirect, url_for, send_from_directory, session, flash, abort
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
            search_query=""
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

    # 検索対象のテキストを作る（通常）
            def card_blob_norm(card: dict) -> str:
                parts = [
                    card.get('name_ja'),
                    card.get('name_ja_kana'),
                    card.get('name_en'),
                    card.get('text_ja'),
                    card.get('text_en'),
                    card.get('illustrator'),
                    card.get('note'),
                    card.get('tribe'),
                    card.get('reference'),
                    card.get('twin_name_ja'),
                    card.get('twin_name_ja_kana'),
                    card.get('twin_name_en'),
                    card.get('twin_text_ja'),
                    card.get('twin_text_en'),
                    card.get('twin_tribe'),

                ]
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
        note_html = markdown.markdown(card.get('note') or '')
        card["regulation_label"] = get_regulation_label(card.get("regulation_type"))
        return render_template('card_detail.html', card=card, note_html=note_html)
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
                f"reference.ilike.{pattern}",
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










