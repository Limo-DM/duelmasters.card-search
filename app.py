# ↓ これ残す
import os
import uuid
import logging
import markdown
import cloudinary
import cloudinary.uploader
from flask import Flask, request, render_template, redirect, url_for, send_from_directory, session, flash, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_, and_, cast
from sqlalchemy.types import Integer
from flask_migrate import Migrate
from functools import wraps
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

# Supabase クライアント（自作ファイル）だけ使う
from supabase_client import supabase







load_dotenv()

app = Flask(__name__)
basedir = os.path.abspath(os.path.dirname(__file__))


app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'static/uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///images.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = os.getenv('SECRET_KEY')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper())
db = SQLAlchemy(app)
migrate = Migrate(app, db)

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

cloudinary.config(
  cloud_name = 'dyn67n0xl',
  api_key = '486683463465663',
  api_secret = '44GsS3fcbe8PN2w6DD_3sM_A6tA',
  secure = True
)


def to_int(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip().replace(',', '')
    if s == '':
        return None
    return int(s) if s.lstrip('-').isdigit() else None


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
    set_name = db.Column(db.String(255))
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

        has_prev = page > 1
        has_next = end < total
        prev_num = page - 1
        next_num = page + 1
        pages = (total + per_page - 1) // per_page

        return render_template(
            'index.html',
            cards=paginated_cards,
            page=page,
            has_prev=has_prev,
            has_next=has_next,
            prev_num=prev_num,
            next_num=next_num,
            pages=pages,
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
    page = request.args.get('page', 1, type=int)
    per_page = 20

    

    try:
        # 全件取得（本番では条件に応じて最適化するのが理想）
        response = supabase.table("Cards").select("*").execute()
        all_cards = response.data or []

        # 絞り込み（Python側で処理）
        if query_text:
            all_cards = [
                card for card in all_cards
                if query_text.lower() in (card.get('name_ja') or '').lower()
                or query_text.lower() in (card.get('name_en') or '').lower()
                or query_text.lower() in (card.get('text_ja') or '').lower()
                or query_text.lower() in (card.get('text_en') or '').lower()
                or query_text.lower() in (card.get('name_ja_kana') or '').lower()
                or query_text.lower() in (card.get('illustrator') or '').lower()
                or query_text.lower() in (card.get('note') or '').lower()
            ]

        if tribe:
            all_cards = [
                card for card in all_cards
                if tribe.lower() in (card.get('tribe') or '').lower()
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

        has_prev = page > 1
        has_next = end < total
        prev_num = page - 1
        next_num = page + 1
        pages = (total + per_page - 1) // per_page

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
            search_query=query_text
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
                f"set_name.ilike.{pattern}",
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




from supabase import create_client, Client

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
        illustrator      = request.form.get('illustrator')
        set_name         = request.form.get('set_name')
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
        image_url = image_url2 = image_public_id = image_public_id2 = None

        if file and file.filename:
            result = cloudinary.uploader.upload(file)
            image_url = result.get('secure_url')
            image_public_id = result.get('public_id')

        if file2 and file2.filename:
            result2 = cloudinary.uploader.upload(file2)
            image_url2 = result2.get('secure_url')
            image_public_id2 = result2.get('public_id')

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
          "illustrator": illustrator,
          "set_name": set_name,
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
            "illustrator": request.form.get('illustrator'),
            "set_name": request.form.get('set_name'),
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

        if file and file.filename:
            result = cloudinary.uploader.upload(file)
            updated_data["image_url"] = result.get("secure_url")
            updated_data["image_public_id"] = result.get("public_id")

        if file2 and file2.filename:
            result2 = cloudinary.uploader.upload(file2)
            updated_data["image_url2"] = result2.get("secure_url")
            updated_data["image_public_id2"] = result2.get("public_id")

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
        # Supabaseから public_id を取り出し（Cloudinary削除のため）
        resp = supabase.table("Cards").select("image_public_id, image_public_id2").eq("id", id).single().execute()
        card = resp.data or {}

        # Cloudinary画像の削除（あれば）
        pub1 = card.get('image_public_id')
        pub2 = card.get('image_public_id2')
        try:
            if pub1: cloudinary.uploader.destroy(pub1)
            if pub2: cloudinary.uploader.destroy(pub2)
        except Exception:
            pass  # 画像削除失敗してもDB削除は続行

        # Supabase行の削除
        supabase.table("Cards").delete().eq("id", id).execute()

        flash('カードを削除しました')
    except Exception as e:
        flash(f'削除時にエラーが発生しました: {e}')

    return redirect(url_for('admin_dashboard'))


@app.route('/delete_image/<int:id>', methods=['POST'])
@admin_required
def delete_image(id):
    # フォームから削除対象を取得: "1" / "2" / "both"（デフォルト both）
    target = (request.form.get('target') or 'both').lower()

    try:
        # まず public_id を取得（Cloudinary削除のため）
        resp = supabase.table("Cards")\
            .select("image_public_id, image_public_id2")\
            .eq("id", id).single().execute()
        row = resp.data or {}

        pub1 = row.get('image_public_id')
        pub2 = row.get('image_public_id2')

        # Cloudinary 側の削除（存在する場合のみ）
        if target in ('1', 'both') and pub1:
            try:
                cloudinary.uploader.destroy(pub1)
            except Exception as ce:
                logging.warning(f"Cloudinary destroy (image1) failed for id={id}: {ce}")

        if target in ('2', 'both') and pub2:
            try:
                cloudinary.uploader.destroy(pub2)
            except Exception as ce:
                logging.warning(f"Cloudinary destroy (image2) failed for id={id}: {ce}")

        # DBのURL/IDをクリア
        update_data = {}
        if target in ('1', 'both'):
            update_data.update({"image_url": None, "image_public_id": None})
        if target in ('2', 'both'):
            update_data.update({"image_url2": None, "image_public_id2": None})

        if update_data:
            supabase.table("Cards").update(update_data).eq("id", id).execute()

        msg = "画像1と画像2を削除しました" if target == 'both' else f"画像{target}を削除しました"
        flash(msg)

    except Exception as e:
        flash(f"画像削除エラー: {e}")

    return redirect(url_for('edit_card', id=id))



if __name__ == '__main__':
    app.run(debug=True)







