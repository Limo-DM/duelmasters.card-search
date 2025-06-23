
import os
import uuid
import logging
import markdown
import cloudinary
import cloudinary.uploader
from flask import Flask, request, render_template, redirect, url_for, send_from_directory, session, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_, and_, cast
from sqlalchemy.types import Integer
from flask_migrate import Migrate
from functools import wraps
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

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
    power = db.Column(db.String(50))
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
    twin_power = db.Column(db.String(50))
    twin_tribe = db.Column(db.String(255))

with app.app_context():
    db.create_all()

@app.route('/')
def index():
    page = request.args.get('page', 1, type=int)
    cards = Card.query.order_by(Card.id.desc()).paginate(page=page, per_page=20)
    return render_template('index.html', cards=cards)


@app.route('/search')
def search():
    tribe = request.args.get('tribe', '').strip()
    civilizations_raw = request.args.get('civilization', '')
    civilizations = [c for c in civilizations_raw.split(',') if c.strip()]
    mode = request.args.get('mode', 'or')
    query_text = request.args.get('query', '').strip()
    selected_fields = request.args.getlist('fields')
    cost_min = request.args.get('cost_min', '').strip()
    cost_max = request.args.get('cost_max', '').strip()
    power_min = request.args.get('power_min', '').strip()
    power_max = request.args.get('power_max', '').strip()
    color_mode = request.args.get('color_mode', 'all')
    page = request.args.get('page', 1, type=int)
    per_page = 20

    query = Card.query.order_by(Card.id.desc())


    if not selected_fields:
        selected_fields = ['name', 'reading', 'text', 'tribe', 'illustrator']

    filters = []
    if query_text:
        if 'name' in selected_fields:
            filters.extend([Card.name_ja.contains(query_text), Card.name_en.contains(query_text), Card.twin_name_ja.contains(query_text), Card.twin_name_en.contains(query_text)])
        if 'reading' in selected_fields:
            filters.extend([Card.name_ja_kana.contains(query_text), Card.twin_name_ja_kana.contains(query_text)])
        if 'text' in selected_fields:
            filters.extend([Card.text_ja.contains(query_text), Card.text_en.contains(query_text), Card.twin_text_ja.contains(query_text), Card.twin_text_en.contains(query_text), Card.note.contains(query_text)])
        if 'tribe' in selected_fields:
            filters.append(Card.tribe.contains(query_text))
        if 'illustrator' in selected_fields:
            filters.append(Card.illustrator.contains(query_text))
    if filters:
        query = query.filter(or_(*filters))

    if cost_min.isdigit():
        query = query.filter(or_(Card.cost >= int(cost_min), Card.twin_cost >= int(cost_min)))
    if cost_max.isdigit():
        query = query.filter(or_(Card.cost <= int(cost_max), Card.twin_cost <= int(cost_max)))
    if power_min.isdigit():
        query = query.filter(or_(cast(Card.power, Integer) >= int(power_min), cast(Card.twin_power, Integer) >= int(power_min)))
    if power_max.isdigit():
        query = query.filter(or_(cast(Card.power, Integer) <= int(power_max), cast(Card.twin_power, Integer) <= int(power_max)))

    if tribe:
        query = query.filter(Card.tribe.contains(tribe))

    apply_civ_filter = civilizations or color_mode in ['mono', 'multi']
    if apply_civ_filter:
        civ_filters = []
        if civilizations:
            for civ in civilizations:
                if color_mode == 'mono':
                    civ_filters.append(and_(or_(Card.civilization == civ, Card.twin_civilization == civ), ~Card.civilization.contains('/'), ~Card.twin_civilization.contains('/')))
                elif color_mode == 'multi':
                    civ_filters.append(or_(and_(Card.civilization.like(f'%{civ}%'), Card.civilization.contains('/')), and_(Card.twin_civilization.like(f'%{civ}%'), Card.twin_civilization.contains('/'))))
                else:
                    civ_filters.append(or_(Card.civilization.like(f'%{civ}%'), Card.twin_civilization.like(f'%{civ}%')))
        else:
            if color_mode == 'mono':
                civ_filters.append(and_(~Card.civilization.contains('/'), ~Card.twin_civilization.contains('/')))
            elif color_mode == 'multi':
                civ_filters.append(or_(Card.civilization.contains('/'), Card.twin_civilization.contains('/')))
        if mode == 'and':
            for f in civ_filters:
                query = query.filter(f)
        else:
            query = query.filter(or_(*civ_filters))

    cards = query.paginate(page=page, per_page=per_page)
    return render_template('index.html', cards=cards)

@app.route('/card/<int:id>')
def card_detail(id):
    card = Card.query.get_or_404(id)

    note_html = markdown.markdown(card.note or '')
    return render_template('card_detail.html', card=card,note_html=note_html)

@app.route('/admin')
@admin_required
def admin_dashboard():
    query = request.args.get('query', '').strip()

    if query:
        filtered_cards = Card.query.filter(
            or_(
                Card.name_ja.contains(query),
                Card.name_en.contains(query),
                Card.tribe.contains(query),
                Card.civilization.contains(query)
            )
        ).all()
    else:
        filtered_cards = Card.query.all()

        db.session.commit()
        # app.pyのこの部分を修正してください
    


    return render_template('admin_dashboard.html', cards=filtered_cards, search_query=query)

@app.route('/upload', methods=['GET', 'POST'])
@admin_required
def upload_file():
    if request.method == 'POST':
        # メインカード情報
        name_ja = request.form.get('name_ja')
        name_ja_kana = request.form.get('name_ja_kana')
        name_en = request.form.get('name_en')
        card_type = request.form.get('card_type')
        civilization = request.form.get('civilization')
        cost = request.form.get('cost', type=int)
        text_ja = request.form.get('text_ja')
        text_en = request.form.get('text_en')
        power = request.form.get('power')
        tribe = request.form.get('tribe')
        illustrator = request.form.get('illustrator')
        set_name = request.form.get('set_name')
        note = request.form.get('note')

        # ツインインパクト情報
        twin_name_ja = request.form.get('twin_name_ja')
        twin_name_ja_kana = request.form.get('twin_name_ja_kana')
        twin_name_en = request.form.get('twin_name_en')
        twin_card_type = request.form.get('twin_card_type')
        twin_civilization = request.form.get('twin_civilization')
        twin_cost = request.form.get('twin_cost', type=int)
        twin_text_ja = request.form.get('twin_text_ja')
        twin_text_en = request.form.get('twin_text_en')
        twin_power = request.form.get('twin_power')
        twin_tribe = request.form.get('twin_tribe')

        # 画像保存（file, file2）
        file = request.files.get('file')
        file2 = request.files.get('file2')
        image_public_id = None
        image_public_id2 = None

        if file and file.filename:
            result = cloudinary.uploader.upload(file)
            image_url = result['secure_url']
            image_public_id = result['public_id']


        if file2 and file2.filename:
            result2 = cloudinary.uploader.upload(file2)
            image_url2 = result2['secure_url']
            image_public_id2 = result2['public_id']

        # カード登録
        new_card = Card(
            name_ja=name_ja,
            name_ja_kana=name_ja_kana,
            name_en=name_en,
            card_type=card_type,
            civilization=civilization,
            cost=cost,
            text_ja=text_ja,
            text_en=text_en,
            power=power,
            tribe=tribe,
            illustrator=illustrator,
            set_name=set_name,
            note=note,
            twin_name_ja=twin_name_ja,
            twin_name_ja_kana=twin_name_ja_kana,
            twin_name_en=twin_name_en,
            twin_card_type=twin_card_type,
            twin_civilization=twin_civilization,
            twin_cost=twin_cost,
            twin_text_ja=twin_text_ja,
            twin_text_en=twin_text_en,
            twin_power=twin_power,
            twin_tribe=twin_tribe,
            image_url=image_url if file else None,
            image_url2=image_url2 if file2 else None,
            image_public_id=image_public_id,        # ← 追加
            image_public_id2=image_public_id2 
        )
        db.session.add(new_card)
        db.session.commit()
        flash('カードを登録しました。')
        return redirect(url_for('admin_dashboard'))

    return render_template('upload.html')


@app.route('/card/<int:id>/edit', methods=['GET', 'POST']) 
def edit_card(id):
    card = Card.query.get_or_404(id)

    if request.method == 'POST':
        # 画像1
        file = request.files.get('file')
        file2 = request.files.get('file2')

     # 画像1：アップロードされたらCloudinaryに上書き、なければ元のURLを使う
        if file and file.filename:
            result = cloudinary.uploader.upload(file)
            card.image_url = result['secure_url']  # ← 上書き
            card.image_public_id = result['public_id']
        # else: なにもしない（そのまま）

        # 画像2：同様
        if file2 and file2.filename:
            result2 = cloudinary.uploader.upload(file2)
            card.image_url2 = result2['secure_url']
            card.image_public_id2 = result2['public_id'] 



        # 通常項目の更新（そのままでOK）
        card.name_ja = request.form['name_ja']
        card.name_ja_kana = request.form['name_ja_kana']
        card.name_en = request.form['name_en']
        card.card_type = request.form['card_type']
        card.civilization = request.form['civilization']
        card.cost = request.form.get('cost', type=int)
        card.text_ja = request.form['text_ja']
        card.text_en = request.form['text_en']
        card.power = request.form['power']
        card.tribe = request.form['tribe']
        card.illustrator = request.form['illustrator']
        card.set_name = request.form['set_name']
        card.note = request.form['note']

        # ツインインパクト
        card.twin_name_ja = request.form['twin_name_ja']
        card.twin_name_ja_kana = request.form['twin_name_ja_kana']
        card.twin_name_en = request.form['twin_name_en']
        card.twin_card_type = request.form['twin_card_type']
        card.twin_civilization = request.form['twin_civilization']
        card.twin_cost = request.form.get('twin_cost', type=int)
        card.twin_text_ja = request.form['twin_text_ja']
        card.twin_text_en = request.form['twin_text_en']
        card.twin_power = request.form['twin_power']
        card.twin_tribe = request.form['twin_tribe']

        db.session.commit()
        flash('カード情報を更新しました')
        return redirect(url_for('admin_dashboard'))

    return render_template('edit_card.html', card=card)


@app.route('/admin/delete/<int:id>', methods=['POST'])
@admin_required
def admin_delete(id):
    card = Card.query.get_or_404(id)
    db.session.delete(card)
    db.session.commit()
    flash('カードを削除しました')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == os.environ.get('ADMIN_PASSWORD'):  # 環境変数などで管理推奨
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

if __name__ == '__main__':
    app.run(debug=True)

@app.route('/delete_image/<int:id>', methods=['POST'])
@admin_required
def delete_image(id):
    card = Card.query.get_or_404(id)

    if card.image_public_id:
        cloudinary.uploader.destroy(card.image_public_id)
    if card.image_public_id2:
        cloudinary.uploader.destroy(card.image_public_id2)

    db.session.delete(card)
    db.session.commit()
    
    
    flash('画像を削除しました')
    return redirect(url_for('edit_card', id=id))





