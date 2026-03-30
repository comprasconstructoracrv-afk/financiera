from flask import Flask, render_template, request, redirect, session
from models import db, Usuario, Credito

app = Flask(__name__)
app.secret_key = "supersecretkey"

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///financiera.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# 🔢 FUNCIÓN DE CÁLCULO
def calcular_cuota(monto, interes, cuotas):
    i = interes / 100
    cuota = monto * (i * (1 + i) ** cuotas) / ((1 + i) ** cuotas - 1)
    return round(cuota, 2)

# 🧱 CREAR BD + USUARIO ADMIN
with app.app_context():
    db.create_all()

    if not Usuario.query.filter_by(username='admin').first():
        nuevo = Usuario(username='admin', password='1234', rol='admin')
        db.session.add(nuevo)
        db.session.commit()

# 🔐 LOGIN
@app.route('/')
def inicio():
    return redirect('/login')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = request.form['username']
        password = request.form['password']

        usuario = Usuario.query.filter_by(username=user, password=password).first()

        if usuario:
            session['user'] = usuario.username
            session['rol'] = usuario.rol
            return redirect('/dashboard')
        else:
            return "Usuario o contraseña incorrectos"

    return render_template('login.html')

# 📊 DASHBOARD
@app.route('/crear_credito', methods=['GET', 'POST'])
def crear_credito():
    if request.method == 'POST':
        cliente = request.form['cliente']
        monto = float(request.form['monto'])
        interes = float(request.form['interes'])
        cuotas = int(request.form['cuotas'])

        cuota = calcular_cuota(monto, interes, cuotas)

        nuevo = Credito(
            cliente=cliente,
            monto=monto,
            interes=interes,
            cuotas=cuotas,
            cuota_mensual=cuota
        )

        db.session.add(nuevo)
        db.session.commit()

        return f"Crédito creado correctamente. Cuota: {cuota}"

    return render_template('crear_credito.html')
@app.route('/dashboard')
def dashboard():
    if 'user' not in session:
        return redirect('/login')

    return render_template(
        'dashboard.html',
        user=session['user'],
        rol=session['rol']
    )
# 🚪 LOGOUT
@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')