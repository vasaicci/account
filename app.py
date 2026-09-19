"""Stockroom - sales & inventory management built with Flask."""
import csv
import io
import os
from datetime import date, datetime, timedelta
from secrets import token_hex

from flask import (Flask, Response, abort, flash, has_request_context, jsonify,
                   redirect, render_template, request, session, url_for)
from flask_login import (LoginManager, UserMixin, current_user, login_user,
                         logout_user)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-in-production")
_db_url = os.environ.get("DATABASE_URL", "sqlite:///inventory.db")
if _db_url.startswith("postgres://"):  # Render/Heroku style URL -> SQLAlchemy style
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    unit = db.Column(db.String(20), default="pcs")
    cost_price = db.Column(db.Float, default=0)
    selling_price = db.Column(db.Float, default=0)
    tax_rate = db.Column(db.Float, default=0)
    stock = db.Column(db.Float, default=0)
    reorder_level = db.Column(db.Float, default=0)
    track_stock = db.Column(db.Boolean, default=True)
    active = db.Column(db.Boolean, default=True)

    @property
    def low(self):
        return self.track_stock and self.stock <= self.reorder_level

    @property
    def value(self):
        return round((self.stock or 0) * (self.cost_price or 0), 2) if self.track_stock else 0


class Party(db.Model):
    """A customer or a vendor."""
    id = db.Column(db.Integer, primary_key=True)
    ptype = db.Column(db.String(10), nullable=False)  # customer | vendor
    name = db.Column(db.String(200), nullable=False)
    company = db.Column(db.String(200), default="")
    email = db.Column(db.String(200), default="")
    phone = db.Column(db.String(50), default="")
    address = db.Column(db.Text, default="")

    @property
    def outstanding(self):
        if self.ptype != "customer":
            return 0
        return round(sum(d.balance for d in self.documents
                         if d.doc_type == "INV" and d.status in ("unpaid", "partial")), 2)


class Document(db.Model):
    """Sales order (SO), invoice (INV) or purchase order (PO)."""
    id = db.Column(db.Integer, primary_key=True)
    doc_type = db.Column(db.String(3), nullable=False, index=True)
    number = db.Column(db.String(30), unique=True, nullable=False)
    party_id = db.Column(db.Integer, db.ForeignKey("party.id"), nullable=False)
    date = db.Column(db.Date, default=date.today)
    due_date = db.Column(db.Date)
    status = db.Column(db.String(20), nullable=False)
    notes = db.Column(db.Text, default="")
    subtotal = db.Column(db.Float, default=0)
    tax = db.Column(db.Float, default=0)
    total = db.Column(db.Float, default=0)
    paid = db.Column(db.Float, default=0)
    source_id = db.Column(db.Integer, db.ForeignKey("document.id"))
    created_by = db.Column(db.String(80))

    party = db.relationship("Party", backref="documents")
    source = db.relationship("Document", remote_side=[id])
    lines = db.relationship("DocLine", backref="doc", cascade="all, delete-orphan")
    payments = db.relationship("Payment", backref="doc", cascade="all, delete-orphan",
                               order_by="Payment.date")

    @property
    def balance(self):
        return round((self.total or 0) - (self.paid or 0), 2)

    @property
    def overdue(self):
        return (self.doc_type == "INV" and self.status in ("unpaid", "partial")
                and self.due_date is not None and self.due_date < date.today())


class DocLine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    doc_id = db.Column(db.Integer, db.ForeignKey("document.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"))
    description = db.Column(db.String(300))
    qty = db.Column(db.Float, nullable=False)
    rate = db.Column(db.Float, nullable=False)
    tax_rate = db.Column(db.Float, default=0)
    amount = db.Column(db.Float, default=0)
    item = db.relationship("Item")


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    doc_id = db.Column(db.Integer, db.ForeignKey("document.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    date = db.Column(db.Date, default=date.today)
    method = db.Column(db.String(30), default="Cash")
    reference = db.Column(db.String(100), default="")


class StockMovement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False)
    change = db.Column(db.Float, nullable=False)
    balance = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(100))
    reference = db.Column(db.String(100), default="")
    created = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.Column(db.String(80))
    item = db.relationship("Item", backref=db.backref("movements", order_by="StockMovement.id.desc()"))


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


# --------------------------------------------------------------------------
# Config & helpers
# --------------------------------------------------------------------------
DOCS = {
    "sales-orders": dict(type="SO", prefix="SO", title="Sales order", plural="Sales orders",
                         party="customer", initial="draft", price="selling_price",
                         statuses=["draft", "confirmed", "invoiced", "cancelled"]),
    "invoices": dict(type="INV", prefix="INV", title="Invoice", plural="Invoices",
                     party="customer", initial="unpaid", price="selling_price",
                     statuses=["unpaid", "partial", "paid", "void"]),
    "purchase-orders": dict(type="PO", prefix="PO", title="Purchase order", plural="Purchase orders",
                            party="vendor", initial="draft", price="cost_price",
                            statuses=["draft", "issued", "received", "cancelled"]),
}
PARTY_KINDS = {"customers": "customer", "vendors": "vendor"}
PAY_METHODS = ["Cash", "Bank transfer", "Card", "Cheque", "UPI / Online"]


def get_cfg(kind):
    cfg = DOCS.get(kind)
    if not cfg:
        abort(404)
    return cfg


def get_doc(cfg, doc_id):
    doc = db.session.get(Document, doc_id)
    if not doc or doc.doc_type != cfg["type"]:
        abort(404)
    return doc


def to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def to_date(v, default=None):
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


def actor():
    if has_request_context() and current_user.is_authenticated:
        return current_user.username
    return "system"


def change_stock(item, delta, reason, ref=""):
    if not item or not item.track_stock:
        return
    item.stock = round((item.stock or 0) + delta, 2)
    db.session.add(StockMovement(
        item=item, change=delta, balance=item.stock, reason=reason, reference=ref,
        user=actor()))


def next_number(cfg):
    n = Document.query.filter_by(doc_type=cfg["type"]).count() + 1
    while Document.query.filter_by(number=f"{cfg['prefix']}-{n:05d}").first():
        n += 1
    return f"{cfg['prefix']}-{n:05d}"


def parse_lines():
    """Read item_id[], qty[], rate[] arrays from the posted form."""
    out = []
    for iid, qty, rate in zip(request.form.getlist("item_id"),
                              request.form.getlist("qty"),
                              request.form.getlist("rate")):
        if not iid:
            continue
        item = db.session.get(Item, int(iid))
        q, r = to_float(qty), to_float(rate, -1)
        if item and q > 0 and r >= 0:
            out.append((item, q, r))
    return out


def stock_errors(pairs):
    need = {}
    for item, q in pairs:
        need[item] = need.get(item, 0) + q
    return [f"Not enough stock for {i.name}: {i.stock:g} on hand, {q:g} needed."
            for i, q in need.items() if i.track_stock and i.stock < q]


def build_doc(cfg, party, lines, doc_date, due, notes):
    doc = Document(doc_type=cfg["type"], number=next_number(cfg), party=party,
                   date=doc_date, due_date=due, notes=notes, status=cfg["initial"],
                   created_by=actor())
    sub = tax = 0.0
    for item, q, r in lines:
        amt = round(q * r, 2)
        sub += amt
        tax += round(amt * (item.tax_rate or 0) / 100, 2)
        doc.lines.append(DocLine(item=item, description=item.name, qty=q, rate=r,
                                 tax_rate=item.tax_rate or 0, amount=amt))
    doc.subtotal, doc.tax = round(sub, 2), round(tax, 2)
    doc.total = round(sub + tax, 2)
    return doc


def deduct_invoice_stock(doc):
    for line in doc.lines:
        change_stock(line.item, -line.qty, "Sale", doc.number)


def refresh_invoice_status(doc):
    if doc.status == "void":
        return
    if doc.paid >= doc.total - 0.005:
        doc.status = "paid"
    elif doc.paid > 0:
        doc.status = "partial"
    else:
        doc.status = "unpaid"


def month_keys(n):
    y, m, keys = date.today().year, date.today().month, []
    for _ in range(n):
        keys.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return keys[::-1]


def monthly_totals(doc_type, statuses, n):
    keys = month_keys(n)
    start = date(keys[0][0], keys[0][1], 1)
    totals = {k: 0.0 for k in keys}
    for d in Document.query.filter(Document.doc_type == doc_type,
                                   Document.status.in_(statuses), Document.date >= start):
        k = (d.date.year, d.date.month)
        if k in totals:
            totals[k] += d.total
    labels = [date(y, m, 1).strftime("%b %y") for y, m in keys]
    return labels, [round(totals[k], 2) for k in keys]


def csv_response(filename, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


# --------------------------------------------------------------------------
# Security: CSRF + login wall
# --------------------------------------------------------------------------
@app.before_request
def guard():
    if request.endpoint == "static":
        return
    if request.method == "POST":
        if not session.get("_csrf") or request.form.get("_csrf") != session["_csrf"]:
            abort(400, "Your session expired. Go back, refresh the page and try again.")
    if not current_user.is_authenticated and request.endpoint != "login":
        return redirect(url_for("login", next=request.path))


@app.context_processor
def inject_globals():
    if "_csrf" not in session:
        session["_csrf"] = token_hex(16)
    return dict(csrf_token=session["_csrf"], today=date.today(), DOCS=DOCS)


@app.template_filter("money")
def money(v):
    return f"{(v or 0):,.2f}"


@app.template_filter("qty")
def qty_fmt(v):
    return f"{(v or 0):g}"


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        user = User.query.filter_by(username=request.form.get("username", "").strip()).first()
        if user and check_password_hash(user.password_hash, request.form.get("password", "")):
            login_user(user)
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        flash("Wrong username or password.", "danger")
    return render_template("login.html")


@app.post("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
def account():
    if request.method == "POST":
        if not check_password_hash(current_user.password_hash, request.form.get("current", "")):
            flash("Current password is incorrect.", "danger")
        elif len(request.form.get("new", "")) < 8:
            flash("New password must be at least 8 characters.", "danger")
        else:
            current_user.set_password(request.form["new"])
            db.session.commit()
            flash("Password changed.", "success")
    return render_template("account.html")


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@app.route("/")
def dashboard():
    today = date.today()
    month_start = today.replace(day=1)
    invoices = Document.query.filter(Document.doc_type == "INV", Document.status != "void").all()
    open_inv = [d for d in invoices if d.status in ("unpaid", "partial")]
    items = Item.query.filter_by(active=True).all()
    labels, sales = monthly_totals("INV", ["unpaid", "partial", "paid"], 6)
    stats = dict(
        sales_month=round(sum(d.total for d in invoices if d.date >= month_start), 2),
        receivables=round(sum(d.balance for d in open_inv), 2),
        overdue=round(sum(d.balance for d in open_inv if d.overdue), 2),
        overdue_count=sum(1 for d in open_inv if d.overdue),
        stock_value=round(sum(i.value for i in items), 2),
        low=[i for i in items if i.low],
        open_so=Document.query.filter(Document.doc_type == "SO",
                                      Document.status.in_(["draft", "confirmed"])).count(),
        open_po=Document.query.filter(Document.doc_type == "PO",
                                      Document.status.in_(["draft", "issued"])).count(),
    )
    recent = (Document.query.filter_by(doc_type="INV").order_by(Document.id.desc()).limit(8).all())
    return render_template("dashboard.html", s=stats, recent=recent, labels=labels, sales=sales)


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------
def fill_item(item):
    f = request.form
    item.sku = f.get("sku", "").strip()
    item.name = f.get("name", "").strip()
    item.description = f.get("description", "").strip()
    item.unit = f.get("unit", "pcs").strip() or "pcs"
    item.cost_price = max(to_float(f.get("cost_price")), 0)
    item.selling_price = max(to_float(f.get("selling_price")), 0)
    item.tax_rate = max(to_float(f.get("tax_rate")), 0)
    item.reorder_level = max(to_float(f.get("reorder_level")), 0)
    item.track_stock = f.get("track_stock") == "on"
    item.active = "on" in f.getlist("active") if "active" in f else True


def item_problem(item):
    if not item.sku or not item.name:
        return "SKU and name are required."
    dup = Item.query.filter(Item.sku == item.sku, Item.id != (item.id or 0)).first()
    return f"SKU {item.sku} is already used by {dup.name}." if dup else None


@app.route("/items")
def items():
    q = request.args.get("q", "").strip()
    flt = request.args.get("filter", "")
    query = Item.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Item.name.ilike(like), Item.sku.ilike(like)))
    rows = query.order_by(Item.name).all()
    if flt == "low":
        rows = [i for i in rows if i.low and i.active]
    elif flt == "inactive":
        rows = [i for i in rows if not i.active]
    else:
        rows = [i for i in rows if i.active]
    return render_template("items.html", items=rows, q=q, flt=flt)


@app.route("/items/new", methods=["GET", "POST"])
def item_new():
    item = Item()
    if request.method == "POST":
        fill_item(item)
        err = item_problem(item)
        if err:
            flash(err, "danger")
        else:
            opening = max(to_float(request.form.get("opening_stock")), 0)
            db.session.add(item)
            db.session.flush()
            if opening:
                change_stock(item, opening, "Opening stock")
            db.session.commit()
            flash(f"{item.name} added.", "success")
            return redirect(url_for("item_detail", item_id=item.id))
    return render_template("item_form.html", item=None, form=request.form)


@app.route("/items/<int:item_id>")
def item_detail(item_id):
    item = db.get_or_404(Item, item_id)
    return render_template("item_detail.html", item=item, movements=item.movements[:100])


@app.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
def item_edit(item_id):
    item = db.get_or_404(Item, item_id)
    if request.method == "POST":
        fill_item(item)
        err = item_problem(item)
        if err:
            db.session.rollback()
            flash(err, "danger")
        else:
            db.session.commit()
            flash("Item updated.", "success")
            return redirect(url_for("item_detail", item_id=item.id))
    return render_template("item_form.html", item=item, form=request.form)


@app.post("/items/<int:item_id>/adjust")
def item_adjust(item_id):
    item = db.get_or_404(Item, item_id)
    delta = to_float(request.form.get("change"))
    if not item.track_stock:
        flash("This item does not track stock.", "danger")
    elif delta == 0:
        flash("Enter a quantity other than zero (use a minus sign to remove stock).", "danger")
    elif item.stock + delta < 0:
        flash(f"Only {item.stock:g} on hand; you can't remove {abs(delta):g}.", "danger")
    else:
        change_stock(item, delta, request.form.get("reason") or "Adjustment",
                     request.form.get("reference", ""))
        db.session.commit()
        flash("Stock adjusted.", "success")
    return redirect(url_for("item_detail", item_id=item.id))


@app.post("/items/<int:item_id>/delete")
def item_delete(item_id):
    item = db.get_or_404(Item, item_id)
    if DocLine.query.filter_by(item_id=item.id).first():
        item.active = False
        flash("This item is used in documents, so it was made inactive instead of deleted.", "info")
    else:
        StockMovement.query.filter_by(item_id=item.id).delete()
        db.session.delete(item)
        flash("Item deleted.", "success")
    db.session.commit()
    return redirect(url_for("items"))


@app.route("/api/items/<int:item_id>")
def api_item(item_id):
    i = db.get_or_404(Item, item_id)
    return jsonify(id=i.id, sku=i.sku, name=i.name, price=i.selling_price, cost=i.cost_price,
                   tax=i.tax_rate, stock=i.stock)


# --------------------------------------------------------------------------
# Customers & vendors
# --------------------------------------------------------------------------
def party_kind(kind):
    ptype = PARTY_KINDS.get(kind)
    if not ptype:
        abort(404)
    return ptype


@app.route("/parties/<kind>")
def parties(kind):
    ptype = party_kind(kind)
    q = request.args.get("q", "").strip()
    query = Party.query.filter_by(ptype=ptype)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Party.name.ilike(like), Party.company.ilike(like),
                                    Party.email.ilike(like)))
    return render_template("parties.html", kind=kind, ptype=ptype, rows=query.order_by(Party.name).all(), q=q)


@app.route("/parties/<kind>/new", methods=["GET", "POST"])
@app.route("/parties/<kind>/<int:party_id>/edit", methods=["GET", "POST"])
def party_form(kind, party_id=None):
    ptype = party_kind(kind)
    party = db.get_or_404(Party, party_id) if party_id else None
    if party and party.ptype != ptype:
        abort(404)
    if request.method == "POST":
        f = request.form
        if not f.get("name", "").strip():
            flash("Name is required.", "danger")
        else:
            party = party or Party(ptype=ptype)
            for field in ("name", "company", "email", "phone", "address"):
                setattr(party, field, f.get(field, "").strip())
            db.session.add(party)
            db.session.commit()
            flash(f"{party.name} saved.", "success")
            return redirect(url_for("party_detail", kind=kind, party_id=party.id))
    return render_template("party_form.html", kind=kind, ptype=ptype, party=party, form=request.form)


@app.route("/parties/<kind>/<int:party_id>")
def party_detail(kind, party_id):
    ptype = party_kind(kind)
    party = db.get_or_404(Party, party_id)
    if party.ptype != ptype:
        abort(404)
    docs = sorted(party.documents, key=lambda d: d.id, reverse=True)
    return render_template("party_detail.html", kind=kind, party=party, docs=docs)


@app.post("/parties/<kind>/<int:party_id>/delete")
def party_delete(kind, party_id):
    party_kind(kind)
    party = db.get_or_404(Party, party_id)
    if party.documents:
        flash("This contact has documents and can't be deleted.", "danger")
        return redirect(url_for("party_detail", kind=kind, party_id=party.id))
    db.session.delete(party)
    db.session.commit()
    flash("Contact deleted.", "success")
    return redirect(url_for("parties", kind=kind))


# --------------------------------------------------------------------------
# Sales orders, invoices, purchase orders
# --------------------------------------------------------------------------
@app.route("/docs/<kind>")
def doc_list(kind):
    cfg = get_cfg(kind)
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()
    query = Document.query.filter_by(doc_type=cfg["type"]).join(Party)
    if status in cfg["statuses"]:
        query = query.filter(Document.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Document.number.ilike(like), Party.name.ilike(like)))
    rows = query.order_by(Document.id.desc()).all()
    return render_template("docs_list.html", kind=kind, cfg=cfg, rows=rows, status=status, q=q)


def render_doc_form(kind, cfg, prefill=None):
    parties_ = Party.query.filter_by(ptype=cfg["party"]).order_by(Party.name).all()
    items_ = [dict(id=i.id, name=i.name, sku=i.sku, price=getattr(i, cfg["price"]) or 0,
                   tax=i.tax_rate or 0, stock=i.stock, track=i.track_stock, unit=i.unit)
              for i in Item.query.filter_by(active=True).order_by(Item.name)]
    return render_template("doc_form.html", kind=kind, cfg=cfg, parties=parties_, items=items_,
                           prefill=prefill or [], form=request.form,
                           default_due=date.today() + timedelta(days=30))


@app.route("/docs/<kind>/new", methods=["GET", "POST"])
def doc_new(kind):
    cfg = get_cfg(kind)
    if request.method == "GET":
        return render_doc_form(kind, cfg)
    f = request.form
    party = db.session.get(Party, int(f.get("party_id") or 0))
    lines = parse_lines()
    errors = []
    if not party or party.ptype != cfg["party"]:
        errors.append(f"Choose a {cfg['party']}.")
    if not lines:
        errors.append("Add at least one line with a quantity above zero.")
    if not errors and cfg["type"] == "INV":
        errors += stock_errors([(i, q) for i, q, _ in lines])
    if errors:
        for e in errors:
            flash(e, "danger")
        return render_doc_form(kind, cfg, [dict(item_id=i.id, qty=q, rate=r) for i, q, r in lines])
    doc = build_doc(cfg, party, lines, to_date(f.get("date"), date.today()),
                    to_date(f.get("due_date")), f.get("notes", "").strip())
    db.session.add(doc)
    if cfg["type"] == "INV":
        deduct_invoice_stock(doc)
    db.session.commit()
    flash(f"{cfg['title']} {doc.number} created.", "success")
    return redirect(url_for("doc_view", kind=kind, doc_id=doc.id))


@app.route("/docs/<kind>/<int:doc_id>")
def doc_view(kind, doc_id):
    cfg = get_cfg(kind)
    doc = get_doc(cfg, doc_id)
    linked = Document.query.filter_by(source_id=doc.id).all()
    return render_template("doc_view.html", kind=kind, cfg=cfg, doc=doc, linked=linked,
                           methods=PAY_METHODS)


@app.post("/docs/<kind>/<int:doc_id>/<action>")
def doc_action(kind, doc_id, action):
    cfg = get_cfg(kind)
    doc = get_doc(cfg, doc_id)
    t, st = cfg["type"], doc.status

    if t == "SO" and action == "confirm" and st == "draft":
        doc.status = "confirmed"
        flash("Sales order confirmed.", "success")

    elif t == "SO" and action == "cancel" and st in ("draft", "confirmed"):
        doc.status = "cancelled"
        flash("Sales order cancelled.", "info")

    elif t == "SO" and action == "convert" and st in ("draft", "confirmed"):
        pairs = [(l.item, l.qty, l.rate) for l in doc.lines]
        errs = stock_errors([(i, q) for i, q, _ in pairs])
        if errs:
            for e in errs:
                flash(e, "danger")
            return redirect(url_for("doc_view", kind=kind, doc_id=doc.id))
        inv = build_doc(DOCS["invoices"], doc.party, pairs, date.today(),
                        date.today() + timedelta(days=30), doc.notes)
        inv.source_id = doc.id
        db.session.add(inv)
        deduct_invoice_stock(inv)
        doc.status = "invoiced"
        db.session.commit()
        flash(f"Invoice {inv.number} created and stock deducted.", "success")
        return redirect(url_for("doc_view", kind="invoices", doc_id=inv.id))

    elif t == "INV" and action == "pay" and st in ("unpaid", "partial"):
        amount = round(to_float(request.form.get("amount")), 2)
        if amount <= 0 or amount > doc.balance + 0.005:
            flash(f"Enter an amount between 0.01 and {doc.balance:,.2f}.", "danger")
        else:
            db.session.add(Payment(doc=doc, amount=amount,
                                   date=to_date(request.form.get("date"), date.today()),
                                   method=request.form.get("method", "Cash"),
                                   reference=request.form.get("reference", "").strip()))
            doc.paid = round(doc.paid + amount, 2)
            refresh_invoice_status(doc)
            flash("Payment recorded.", "success")

    elif t == "INV" and action == "void" and st != "void":
        if doc.paid > 0:
            flash("This invoice has payments. Remove them before voiding.", "danger")
        else:
            for l in doc.lines:
                change_stock(l.item, l.qty, "Invoice voided", doc.number)
            doc.status = "void"
            flash("Invoice voided and stock returned.", "info")

    elif t == "PO" and action == "issue" and st == "draft":
        doc.status = "issued"
        flash("Purchase order marked as issued.", "success")

    elif t == "PO" and action == "cancel" and st in ("draft", "issued"):
        doc.status = "cancelled"
        flash("Purchase order cancelled.", "info")

    elif t == "PO" and action == "receive" and st in ("draft", "issued"):
        for l in doc.lines:
            change_stock(l.item, l.qty, "Purchase received", doc.number)
            if l.item and l.rate:
                l.item.cost_price = l.rate
        doc.status = "received"
        flash("Stock received and cost prices updated.", "success")

    else:
        flash("That action isn't available for this document's current status.", "danger")

    db.session.commit()
    return redirect(url_for("doc_view", kind=kind, doc_id=doc.id))


@app.post("/docs/<kind>/<int:doc_id>/payments/<int:pay_id>/delete")
def payment_delete(kind, doc_id, pay_id):
    cfg = get_cfg(kind)
    doc = get_doc(cfg, doc_id)
    pay = db.get_or_404(Payment, pay_id)
    if pay.doc_id != doc.id:
        abort(404)
    doc.paid = round(doc.paid - pay.amount, 2)
    db.session.delete(pay)
    refresh_invoice_status(doc)
    db.session.commit()
    flash("Payment removed.", "info")
    return redirect(url_for("doc_view", kind=kind, doc_id=doc.id))


# --------------------------------------------------------------------------
# Reports & exports
# --------------------------------------------------------------------------
@app.route("/reports")
def reports():
    labels, sales = monthly_totals("INV", ["unpaid", "partial", "paid"], 12)
    _, purchases = monthly_totals("PO", ["received"], 12)
    top = (db.session.query(Item.name, func.sum(DocLine.qty), func.sum(DocLine.amount))
           .join(DocLine, DocLine.item_id == Item.id)
           .join(Document, Document.id == DocLine.doc_id)
           .filter(Document.doc_type == "INV", Document.status != "void")
           .group_by(Item.id).order_by(func.sum(DocLine.amount).desc()).limit(10).all())
    aging = {"Not yet due": 0.0, "1-30 days": 0.0, "31-60 days": 0.0, "Over 60 days": 0.0}
    for d in Document.query.filter(Document.doc_type == "INV", Document.status.in_(["unpaid", "partial"])):
        late = (date.today() - d.due_date).days if d.due_date else 0
        key = ("Not yet due" if late <= 0 else "1-30 days" if late <= 30
               else "31-60 days" if late <= 60 else "Over 60 days")
        aging[key] += d.balance
    valuation = Item.query.filter_by(active=True, track_stock=True).order_by(Item.name).all()
    return render_template("reports.html", labels=labels, sales=sales, purchases=purchases, top=top,
                           aging=aging, valuation=valuation,
                           total_value=round(sum(i.value for i in valuation), 2))


@app.route("/export/<what>.csv")
def export(what):
    if what == "items":
        return csv_response("items.csv",
                            ["SKU", "Name", "Unit", "Cost", "Price", "Tax %", "Stock", "Reorder level", "Value"],
                            [[i.sku, i.name, i.unit, i.cost_price, i.selling_price, i.tax_rate, i.stock,
                              i.reorder_level, i.value] for i in Item.query.order_by(Item.name)])
    if what == "invoices":
        return csv_response("invoices.csv",
                            ["Number", "Date", "Due", "Customer", "Status", "Subtotal", "Tax", "Total", "Paid", "Balance"],
                            [[d.number, d.date, d.due_date, d.party.name, d.status, d.subtotal, d.tax, d.total,
                              d.paid, d.balance] for d in Document.query.filter_by(doc_type="INV").order_by(Document.id)])
    if what in ("customers", "vendors"):
        ptype = PARTY_KINDS[what]
        return csv_response(f"{what}.csv", ["Name", "Company", "Email", "Phone", "Address"],
                            [[p.name, p.company, p.email, p.phone, p.address]
                             for p in Party.query.filter_by(ptype=ptype).order_by(Party.name)])
    abort(404)


# --------------------------------------------------------------------------
# Setup & CLI
# --------------------------------------------------------------------------
def init_db():
    db.create_all()
    if not User.query.first():
        admin = User(username="admin")
        admin.set_password(os.environ.get("ADMIN_PASSWORD", "admin123"))
        db.session.add(admin)
        db.session.commit()


@app.cli.command("seed-demo")
def seed_demo():
    """Add sample items, customers and vendors."""
    init_db()
    if Item.query.first():
        print("Data already exists; skipping.")
        return
    demo = [("LAP-001", "Laptop 14\"", "pcs", 620, 799, 18, 15, 5),
            ("MOU-010", "Wireless mouse", "pcs", 6, 14.5, 18, 120, 30),
            ("KEY-020", "Mechanical keyboard", "pcs", 28, 59, 18, 40, 15),
            ("MON-027", "27\" monitor", "pcs", 140, 219, 18, 12, 10),
            ("CAB-005", "USB-C cable 1m", "pcs", 1.8, 6.5, 18, 200, 50)]
    for sku, name, unit, cost, price, tax, stock, reorder in demo:
        item = Item(sku=sku, name=name, unit=unit, cost_price=cost, selling_price=price,
                    tax_rate=tax, reorder_level=reorder)
        db.session.add(item)
        db.session.flush()
        change_stock(item, stock, "Opening stock")
    for name, company, email in [("Priya Sharma", "Northwind Traders", "priya@northwind.example"),
                                 ("Daniel Okoye", "Brightside Retail", "daniel@brightside.example")]:
        db.session.add(Party(ptype="customer", name=name, company=company, email=email))
    for name, company, email in [("Mei Lin", "Pacific Components", "mei@pacific.example")]:
        db.session.add(Party(ptype="vendor", name=name, company=company, email=email))
    db.session.commit()
    print("Demo data added.")


with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(debug=True)
