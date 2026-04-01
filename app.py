from flask import Flask, render_template, request, redirect, session
from models import db, Usuario, Credito, Cuota, Pago, ConfiguracionTasa, TasaPeriodo
from datetime import datetime, date, timedelta
import calendar
import os

app = Flask(__name__)
app.secret_key = "supersecretkey"
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///financiera.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

@app.template_filter('cop')
def formato_cop(valor):
    if valor is None:
        return "$ 0"
    valor_redondeado = int(round(valor))
    texto = f"{valor_redondeado:,}".replace(",", ".")
    return f"$ {texto}"

# 🔢 FUNCIÓN DE CÁLCULO
def calcular_cuota(monto, interes, cuotas):
    i = interes / 100
    cuota = monto * (i * (1 + i) ** cuotas) / ((1 + i) ** cuotas - 1)
    return round(cuota, 2)

def sumar_meses(fecha, meses):
    mes = fecha.month - 1 + meses
    anio = fecha.year + mes // 12
    mes = mes % 12 + 1
    dia = min(fecha.day, calendar.monthrange(anio, mes)[1])
    return fecha.replace(year=anio, month=mes, day=dia)

def convertir_tasa_anual_a_mensual(tasa_anual):
    return round((((1 + (tasa_anual / 100)) ** (1/12)) - 1) * 100, 6)

def convertir_tasa_mensual_a_diaria(tasa_mensual):
    return round((tasa_mensual / 100) / 30, 10)

def obtener_o_crear_tasa_periodo(anio, mes, tasa_anual_base):
    tasa = TasaPeriodo.query.filter_by(anio=anio, mes=mes).first()

    if not tasa:
        tasa_mensual = convertir_tasa_anual_a_mensual(tasa_anual_base)
        tasa_diaria = convertir_tasa_mensual_a_diaria(tasa_mensual)

        tasa = TasaPeriodo(
            anio=anio,
            mes=mes,
            tasa_anual=tasa_anual_base,
            tasa_mensual=tasa_mensual,
            tasa_diaria=tasa_diaria
        )
        db.session.add(tasa)
        db.session.flush()

    return tasa


def generar_cuotas(credito_id, monto, interes, cuotas, fecha_base):
    saldo = round(monto, 2)
    tasa = interes / 100
    cuota_fija = calcular_cuota(monto, interes, cuotas)

    for n in range(cuotas):
        saldo_inicial = round(saldo, 2)
        interes_mes = round(saldo_inicial * tasa, 2)
        capital = round(cuota_fija - interes_mes, 2)
        saldo = round(saldo_inicial - capital, 2)

        if saldo < 0:
            saldo = 0

        fecha_pago = sumar_meses(fecha_base, n)

        nueva_cuota = Cuota(
            credito_id=credito_id,
            numero=n + 1,
            fecha_pago=fecha_pago,
            valor_cuota=cuota_fija,
            saldo_inicial=saldo_inicial,
            capital=capital,
            interes=interes_mes,
            saldo_restante=saldo,
            saldo_pendiente=cuota_fija,
            dias_mora=0,
            interes_mora=0,
            total_cobro=cuota_fija,
            estado='PENDIENTE'
        )
        db.session.add(nueva_cuota)

def ultimo_dia_mes(fecha):
    ultimo = calendar.monthrange(fecha.year, fecha.month)[1]
    return date(fecha.year, fecha.month, ultimo)

def actualizar_mora_credito(credito, fecha_corte=None):
    if fecha_corte is None:
        fecha_corte = datetime.utcnow().date()

    cuotas = Cuota.query.filter_by(credito_id=credito.id).order_by(Cuota.numero).all()

    for cuota in cuotas:
        # Si la cuota ya no debe nada, debe quedar pagada sí o sí
        if cuota.saldo_pendiente is None or cuota.saldo_pendiente <= 0:
            cuota.saldo_pendiente = 0
            cuota.dias_mora = 0
            cuota.interes_mora = 0
            cuota.total_cobro = 0
            cuota.estado = 'PAGADA'
            continue

        # Reset base
        cuota.dias_mora = 0
        cuota.interes_mora = 0
        cuota.total_cobro = cuota.saldo_pendiente

        fecha_vencimiento = cuota.fecha_pago.date()
        fecha_inicio_mora = fecha_vencimiento + timedelta(days=1)

        # Si todavía no entra en mora
        if fecha_corte < fecha_inicio_mora:
            if cuota.estado != 'ABONO':
                cuota.estado = 'PENDIENTE'
            cuota.dias_mora = 0
            cuota.interes_mora = 0
            cuota.total_cobro = cuota.saldo_pendiente
            continue

        # Calcular días de mora
        dias_mora = (fecha_corte - fecha_inicio_mora).days + 1
        cuota.dias_mora = dias_mora

        # Calcular mora por tramos mensuales
        mora_total = 0.0
        cursor = fecha_inicio_mora

        while cursor <= fecha_corte:
            fin_mes = min(ultimo_dia_mes(cursor), fecha_corte)
            dias_tramo = (fin_mes - cursor).days + 1

            tasa_periodo = TasaPeriodo.query.filter_by(
                anio=cursor.year,
                mes=cursor.month
            ).first()

            if tasa_periodo:
                mora_tramo = cuota.saldo_pendiente * tasa_periodo.tasa_diaria * dias_tramo
                mora_total += mora_tramo

            cursor = fin_mes + timedelta(days=1)

        cuota.interes_mora = round(mora_total, 2)
        cuota.total_cobro = round(cuota.saldo_pendiente + cuota.interes_mora, 2)

        if cuota.estado != 'ABONO':
            cuota.estado = 'EN MORA'



def recalcular_cuotas_pendientes(credito, cuota_actual_numero, fecha_base):
    cuotas_pendientes = Cuota.query.filter(
        Cuota.credito_id == credito.id,
        Cuota.numero > cuota_actual_numero,
        Cuota.estado != 'PAGADA'
    ).order_by(Cuota.numero).all()

    cantidad_pendientes = len(cuotas_pendientes)

    if cantidad_pendientes <= 0:
        credito.cuota_mensual = 0
        return

    for cuota in cuotas_pendientes:
        db.session.delete(cuota)

    db.session.flush()

    saldo = round(credito.saldo_actual, 2)
    tasa_credito = credito.interes / 100
    nueva_cuota = calcular_cuota(saldo, credito.interes, cantidad_pendientes)
    credito.cuota_mensual = nueva_cuota

    config_tasa = ConfiguracionTasa.query.filter_by(nombre='TASA_MORA').first()

    for i in range(cantidad_pendientes):
        saldo_inicial = round(saldo, 2)
        interes_mes = round(saldo_inicial * tasa_credito, 2)
        capital = round(nueva_cuota - interes_mes, 2)
        saldo = round(saldo_inicial - capital, 2)

        if saldo < 0:
            saldo = 0

        fecha_pago = sumar_meses(fecha_base, i + 1)

        tasa_periodo = obtener_o_crear_tasa_periodo(
            anio=fecha_pago.year,
            mes=fecha_pago.month,
            tasa_anual_base=config_tasa.tasa_anual
        )

        nueva = Cuota(
            credito_id=credito.id,
            numero=cuota_actual_numero + i + 1,
            fecha_pago=fecha_pago,
            valor_cuota=nueva_cuota,
            saldo_inicial=saldo_inicial,
            capital=capital,
            interes=interes_mes,
            saldo_restante=saldo,
            saldo_pendiente=nueva_cuota,
            tasa_mora_mensual_cuota=tasa_periodo.tasa_mensual,
            dias_mora=0,
            interes_mora=0,
            total_cobro=nueva_cuota,
            estado='PENDIENTE'
        )
        db.session.add(nueva)

def generar_cuotas(credito_id, monto, interes, cuotas, fecha_base):
    saldo = round(monto, 2)
    tasa_credito = interes / 100
    cuota_fija = calcular_cuota(monto, interes, cuotas)

    config_tasa = ConfiguracionTasa.query.filter_by(nombre='TASA_MORA').first()

    for n in range(cuotas):
        saldo_inicial = round(saldo, 2)
        interes_mes = round(saldo_inicial * tasa_credito, 2)
        capital = round(cuota_fija - interes_mes, 2)
        saldo = round(saldo_inicial - capital, 2)

        if saldo < 0:
            saldo = 0

        fecha_pago = sumar_meses(fecha_base, n)

        tasa_periodo = obtener_o_crear_tasa_periodo(
            anio=fecha_pago.year,
            mes=fecha_pago.month,
            tasa_anual_base=config_tasa.tasa_anual
        )

        nueva_cuota = Cuota(
            credito_id=credito_id,
            numero=n + 1,
            fecha_pago=fecha_pago,
            valor_cuota=cuota_fija,
            saldo_inicial=saldo_inicial,
            capital=capital,
            interes=interes_mes,
            saldo_restante=saldo,
            saldo_pendiente=cuota_fija,
            tasa_mora_mensual_cuota=tasa_periodo.tasa_mensual,
            dias_mora=0,
            interes_mora=0,
            total_cobro=cuota_fija,
            estado='PENDIENTE'
        )
        db.session.add(nueva_cuota)


# 🧱 CREAR BD + USUARIO ADMIN
with app.app_context():
    db.create_all()

    if not Usuario.query.filter_by(username='admin').first():
        nuevo = Usuario(username='admin', password='1234', rol='admin')
        db.session.add(nuevo)
        db.session.commit()

    if not ConfiguracionTasa.query.filter_by(nombre='TASA_MORA').first():
        tasa_anual = 25.52
        tasa_mensual = convertir_tasa_anual_a_mensual(tasa_anual)
        tasa_diaria = convertir_tasa_mensual_a_diaria(tasa_mensual)

        config = ConfiguracionTasa(
            nombre='TASA_MORA',
            tasa_anual=tasa_anual,
            tasa_mensual=tasa_mensual,
            tasa_diaria=tasa_diaria
        )
        db.session.add(config)
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
    if 'user' not in session:
        return redirect('/login')

    if request.method == 'POST':
        cliente = request.form['cliente']
        monto = float(request.form['monto'])
        interes = float(request.form['interes'])
        cuotas = int(request.form['cuotas'])
        fecha_credito = datetime.strptime(request.form['fecha_credito'], '%Y-%m-%d')

        abono_inicial_texto = request.form.get('abono_inicial', '').strip()
        abono_inicial = float(abono_inicial_texto) if abono_inicial_texto else 0

        monto_financiado = monto - abono_inicial

        if monto_financiado <= 0:
            return "El monto financiado debe ser mayor que cero"

        cuota = calcular_cuota(monto_financiado, interes, cuotas)

        config_tasa = ConfiguracionTasa.query.filter_by(nombre='TASA_MORA').first()

        nuevo = Credito(
            cliente=cliente,
            monto=monto,
            abono_inicial=abono_inicial,
            monto_financiado=monto_financiado,
            saldo_actual=monto_financiado,
            interes=interes,
            cuotas=cuotas,
            cuota_mensual=cuota,
            tasa_mora_anual=config_tasa.tasa_anual,
            tasa_mora_mensual=config_tasa.tasa_mensual,
            tasa_mora_diaria=config_tasa.tasa_diaria,
            fecha_creacion=fecha_credito
        )

        db.session.add(nuevo)
        db.session.commit()

        generar_cuotas(nuevo.id, monto_financiado, interes, cuotas, fecha_credito)
        db.session.commit()

        return redirect('/ver_creditos')

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

@app.route('/ver_creditos')
def ver_creditos():
    if 'user' not in session:
        return redirect('/login')

    creditos = Credito.query.all()
    return render_template('ver_creditos.html', creditos=creditos)


@app.route('/ver_cuotas/<int:credito_id>')
def ver_cuotas(credito_id):
    if 'user' not in session:
        return redirect('/login')

    credito = Credito.query.get_or_404(credito_id)

    # Mora calculada a hoy
    actualizar_mora_credito(credito, datetime.utcnow().date())
    db.session.commit()

    cuotas = Cuota.query.filter_by(credito_id=credito_id).order_by(Cuota.numero).all()

    pagos_por_cuota = {}
    for cuota in cuotas:
        pagos = Pago.query.filter_by(cuota_id=cuota.id).order_by(Pago.fecha).all()
        pagos_por_cuota[cuota.id] = pagos

    return render_template(
        'ver_cuotas.html',
        credito=credito,
        cuotas=cuotas,
        pagos_por_cuota=pagos_por_cuota
    )

@app.route('/pagar_cuota/<int:cuota_id>', methods=['GET', 'POST'])
def pagar_cuota(cuota_id):
    if 'user' not in session:
        return redirect('/login')

    cuota = Cuota.query.get_or_404(cuota_id)
    credito = Credito.query.get_or_404(cuota.credito_id)

    if request.method == 'POST':
        valor_pago = float(request.form['valor'])
        fecha_pago = datetime.strptime(request.form['fecha_pago'], '%Y-%m-%d')
        medio_pago = request.form['medio_pago']

        if medio_pago == 'OTRO':
            medio_pago_otro = request.form.get('medio_pago_otro', '').strip()
            if not medio_pago_otro:
                return "Debes escribir el otro medio de pago"
            medio_pago = medio_pago_otro

        if valor_pago <= 0:
            return "El pago debe ser mayor que cero"

        # Recalcular mora con la FECHA REAL DEL PAGO
        actualizar_mora_credito(credito, fecha_pago.date())
        db.session.commit()

        # Recargar cuota actualizada con mora a esa fecha
        cuota = Cuota.query.get_or_404(cuota_id)

        pago = Pago(
            cuota_id=cuota.id,
            fecha=fecha_pago,
            valor=valor_pago,
            medio_pago=medio_pago
        )
        db.session.add(pago)

        restante = valor_pago

        # 1. Primero cubrir mora
        if cuota.interes_mora > 0:
            if restante >= cuota.interes_mora:
                restante -= cuota.interes_mora
                cuota.interes_mora = 0
            else:
                cuota.interes_mora = round(cuota.interes_mora - restante, 2)
                restante = 0

        # 2. Luego cubrir saldo pendiente de la cuota
        if restante > 0:
            if restante < cuota.saldo_pendiente:
                cuota.saldo_pendiente = round(cuota.saldo_pendiente - restante, 2)
                cuota.estado = 'ABONO'
                restante = 0
            else:
                restante = round(restante - cuota.saldo_pendiente, 2)
                cuota.saldo_pendiente = 0
                cuota.estado = 'PAGADA'
                credito.saldo_actual = round(credito.saldo_actual - cuota.capital, 2)

        # 3. Si sobra, va a capital y recalcula futuras
        if restante > 0:
            credito.saldo_actual = round(credito.saldo_actual - restante, 2)

            if credito.saldo_actual < 0:
                credito.saldo_actual = 0

            recalcular_cuotas_pendientes(
                credito=credito,
                cuota_actual_numero=cuota.numero,
                fecha_base=cuota.fecha_pago
            )

        # 4. Normalizar estado final
        if cuota.saldo_pendiente <= 0:
            cuota.saldo_pendiente = 0
            cuota.dias_mora = 0
            cuota.interes_mora = 0
            cuota.total_cobro = 0
            cuota.estado = 'PAGADA'
        else:
            cuota.total_cobro = round(cuota.saldo_pendiente + cuota.interes_mora, 2)

        db.session.commit()
        return redirect(f'/ver_cuotas/{cuota.credito_id}')

    # Si solo entra a ver la pantalla de pago, mostrar mora a hoy
    actualizar_mora_credito(credito, datetime.utcnow().date())
    db.session.commit()
    cuota = Cuota.query.get_or_404(cuota_id)

    return render_template('pagar_cuota.html', cuota=cuota)


@app.route('/configuracion_tasa', methods=['GET', 'POST'])
def configuracion_tasa():
    if 'user' not in session:
        return redirect('/login')

    config = ConfiguracionTasa.query.filter_by(nombre='TASA_MORA').first()

    if request.method == 'POST':
        tasa_anual = float(request.form['tasa_anual'])
        tasa_mensual = convertir_tasa_anual_a_mensual(tasa_anual)
        tasa_diaria = convertir_tasa_mensual_a_diaria(tasa_mensual)

        config.tasa_anual = tasa_anual
        config.tasa_mensual = tasa_mensual
        config.tasa_diaria = tasa_diaria

        db.session.commit()
        return redirect('/configuracion_tasa')

    return render_template('configuracion_tasa.html', config=config)