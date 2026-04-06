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

def limpiar_valor_moneda(valor):
    if valor is None:
        return 0.0

    texto = str(valor).strip()
    texto = texto.replace('$', '').replace('.', '').replace(',', '').replace(' ', '')

    if texto == '':
        return 0.0

    return float(texto)


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

def obtener_tasa_periodo(anio, mes):
    tasa_periodo = TasaPeriodo.query.filter_by(anio=anio, mes=mes).first()

    if tasa_periodo:
        return tasa_periodo

    return ConfiguracionTasa.query.first()


def actualizar_mora_credito(credito):
    hoy = date.today()

    for cuota in credito.cuotas:
        if cuota.estado in ['PAGADA', 'LIQUIDADA']:
            cuota.dias_mora = 0
            cuota.interes_mora = 0
            cuota.total_cobro = round(cuota.saldo_pendiente or 0)
            continue

        saldo_base = cuota.saldo_pendiente if cuota.saldo_pendiente and cuota.saldo_pendiente > 0 else cuota.valor_cuota

        tasa = obtener_tasa_periodo(cuota.fecha_pago.year, cuota.fecha_pago.month)
        tasa_mensual = tasa.tasa_mensual if tasa else 0
        tasa_diaria = tasa.tasa_diaria if tasa else 0

        cuota.tasa_mora_mensual_cuota = tasa_mensual
        cuota.porcentaje_mora_aplicado = tasa_mensual

        if hoy > cuota.fecha_pago and saldo_base > 0:
            dias_mora = (hoy - cuota.fecha_pago).days
            interes_mora = saldo_base * tasa_diaria * dias_mora

            cuota.dias_mora = dias_mora
            cuota.interes_mora = round(interes_mora)
            cuota.total_cobro = round(saldo_base + cuota.interes_mora)
            cuota.estado = 'EN MORA'
        else:
            cuota.dias_mora = 0
            cuota.interes_mora = 0
            cuota.total_cobro = round(saldo_base)
            cuota.estado = 'PENDIENTE'


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
        numero_pagare = request.form['numero_pagare'].strip()
        monto = limpiar_valor_moneda(request.form['monto'])
        interes = float(request.form['interes'])
        cuotas = int(request.form['cuotas'])
        fecha_credito = datetime.strptime(request.form['fecha_credito'], '%Y-%m-%d')

        abono_inicial_texto = request.form.get('abono_inicial', '').strip()
        abono_inicial = limpiar_valor_moneda(abono_inicial_texto) if abono_inicial_texto else 0

        monto_financiado = monto - abono_inicial

        if monto_financiado <= 0:
            return "El monto financiado debe ser mayor que cero"

        cuota = calcular_cuota(monto_financiado, interes, cuotas)

        config_tasa = ConfiguracionTasa.query.filter_by(nombre='TASA_MORA').first()

        nuevo = Credito(
            numero_pagare=numero_pagare,
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
    actualizar_mora_credito(credito)
    db.session.commit()

    cuotas = Cuota.query.filter_by(credito_id=credito_id).order_by(Cuota.numero).all()

    pagos_por_cuota = {}
    ultimo_pago = None

    for cuota in cuotas:
        pagos = Pago.query.filter_by(cuota_id=cuota.id).order_by(Pago.fecha).all()
        pagos_por_cuota[cuota.id] = pagos

        if pagos:
            ultimo_pago_cuota = pagos[-1]
            if ultimo_pago is None or ultimo_pago_cuota.fecha > ultimo_pago.fecha:
                ultimo_pago = ultimo_pago_cuota

    cuota_pendiente_total = round(
        sum((cuota.saldo_pendiente or 0) for cuota in cuotas),
        2
    )
    mora_total = round(
        sum((cuota.interes_mora or 0) for cuota in cuotas),
        2
    )
    deuda_total_fecha = round(cuota_pendiente_total + mora_total, 2)

    if any(c.estado == 'EN MORA' for c in cuotas):
        estado_credito = 'EN MORA'
    elif all(c.estado in ['PAGADA', 'LIQUIDADA'] for c in cuotas):
        estado_credito = 'CANCELADO'
    elif any(c.estado == 'ABONO' for c in cuotas):
        estado_credito = 'CON ABONOS'
    else:
        estado_credito = 'AL DÍA'

    return render_template(
        'ver_cuotas.html',
        credito=credito,
        cuotas=cuotas,
        pagos_por_cuota=pagos_por_cuota,
        ultimo_pago=ultimo_pago,
        estado_credito=estado_credito,
        cuota_pendiente_total=cuota_pendiente_total,
        mora_total=mora_total,
        deuda_total_fecha=deuda_total_fecha
    )


@app.route('/pagar_cuota/<int:cuota_id>', methods=['GET', 'POST'])
def pagar_cuota(cuota_id):
    if 'user' not in session:
        return redirect('/login')

    cuota = Cuota.query.get_or_404(cuota_id)
    credito = Credito.query.get_or_404(cuota.credito_id)

    if request.method == 'POST':
        valor_pago = limpiar_valor_moneda(request.form['valor'])
        fecha_pago = datetime.strptime(request.form['fecha_pago'], '%Y-%m-%d')
        medio_pago = request.form['medio_pago']

        if medio_pago == 'OTRO':
            medio_pago_otro = request.form.get('medio_pago_otro', '').strip()
            if not medio_pago_otro:
                return "Debes escribir el otro medio de pago"
            medio_pago = medio_pago_otro

        if valor_pago <= 0:
            return "El pago debe ser mayor que cero"

        # Recalcular mora exactamente a la fecha del pago
        actualizar_mora_credito(credito, fecha_pago.date())
        db.session.commit()

        # Recargar cuota actualizada
        cuota = Cuota.query.get_or_404(cuota_id)

        pago = Pago(
            cuota_id=cuota.id,
            fecha=fecha_pago,
            valor=valor_pago,
            medio_pago=medio_pago
        )
        db.session.add(pago)

        restante = round(valor_pago, 2)
        hubo_abono_extra_capital = False

        valor_cuota_hoy = round(cuota.valor_cuota, 2)
        mora_hoy = round(cuota.interes_mora, 2)

        # 1. Cubrir primero la cuota base
        if cuota.saldo_pendiente > 0:
            aplicado_cuota = min(restante, round(cuota.saldo_pendiente, 2))
            cuota.saldo_pendiente = round(cuota.saldo_pendiente - aplicado_cuota, 2)
            restante = round(restante - aplicado_cuota, 2)

            # Si ya cubrió la cuota completa, bajar capital contractual exacto
            if cuota.saldo_pendiente <= 0:
                cuota.saldo_pendiente = 0
                credito.saldo_actual = round(credito.saldo_actual - cuota.capital, 2)

        # 2. Luego cubrir mora
        if restante > 0 and cuota.interes_mora > 0:
            aplicado_mora = min(restante, round(cuota.interes_mora, 2))
            cuota.interes_mora = round(cuota.interes_mora - aplicado_mora, 2)
            restante = round(restante - aplicado_mora, 2)

        # 3. Solo es prepago real si pagó más de cuota + mora real de esa fecha
        total_exigible = round(valor_cuota_hoy + mora_hoy, 2)
        if valor_pago > total_exigible and restante > 0:
            credito.saldo_actual = round(credito.saldo_actual - restante, 2)

            if credito.saldo_actual < 0:
                credito.saldo_actual = 0

            hubo_abono_extra_capital = True
            restante = 0

        # Normalización numérica
        cuota.saldo_pendiente = round(max(cuota.saldo_pendiente, 0), 2)
        cuota.interes_mora = round(max(cuota.interes_mora, 0), 2)

        # Recalcular cuotas SOLO si sí hubo abono extra a capital real
        if hubo_abono_extra_capital:
            recalcular_cuotas_pendientes(
                credito=credito,
                cuota_actual_numero=cuota.numero,
                fecha_base=cuota.fecha_pago
            )

        # Normalizar estado final
        if cuota.saldo_pendiente <= 0 and cuota.interes_mora <= 0:
            cuota.saldo_pendiente = 0
            cuota.dias_mora = 0
            cuota.interes_mora = 0
            cuota.total_cobro = 0
            cuota.estado = 'PAGADA'

        elif cuota.saldo_pendiente <= 0 and cuota.interes_mora > 0:
            cuota.saldo_pendiente = 0
            cuota.total_cobro = round(cuota.interes_mora, 2)
            cuota.estado = 'ABONO'

        else:
            cuota.total_cobro = round(cuota.saldo_pendiente + cuota.interes_mora, 2)
            if cuota.dias_mora > 0:
                cuota.estado = 'EN MORA'
            else:
                cuota.estado = 'ABONO'

        db.session.commit()
        return redirect(f'/ver_cuotas/{cuota.credito_id}')

    actualizar_mora_credito(credito, datetime.utcnow().date())
    db.session.commit()
    cuota = Cuota.query.get_or_404(cuota_id)

    return render_template('pagar_cuota.html', cuota=cuota)


@app.route('/configuracion_tasa', methods=['GET', 'POST'])
def configuracion_tasa():
    if 'user' not in session:
        return redirect('/login')

    if request.method == 'POST':
        anio = int(request.form['anio'])
        mes = int(request.form['mes'])
        tasa_anual = float(str(request.form['tasa_anual']).replace(',', '.'))

        tasa_mensual = convertir_tasa_anual_a_mensual(tasa_anual)
        tasa_diaria = convertir_tasa_mensual_a_diaria(tasa_mensual)

        tasa_periodo = TasaPeriodo.query.filter_by(anio=anio, mes=mes).first()

        if not tasa_periodo:
            tasa_periodo = TasaPeriodo(
                anio=anio,
                mes=mes,
                tasa_anual=tasa_anual,
                tasa_mensual=tasa_mensual,
                tasa_diaria=tasa_diaria
            )
            db.session.add(tasa_periodo)
        else:
            tasa_periodo.tasa_anual = tasa_anual
            tasa_periodo.tasa_mensual = tasa_mensual
            tasa_periodo.tasa_diaria = tasa_diaria

        # respaldo global opcional
        configuracion = ConfiguracionTasa.query.first()
        if not configuracion:
            configuracion = ConfiguracionTasa(
                nombre='TASA_MORA',
                tasa_anual=tasa_anual,
                tasa_mensual=tasa_mensual,
                tasa_diaria=tasa_diaria
            )
            db.session.add(configuracion)
        else:
            configuracion.tasa_anual = tasa_anual
            configuracion.tasa_mensual = tasa_mensual
            configuracion.tasa_diaria = tasa_diaria

        db.session.commit()
        flash(f'Tasa guardada correctamente para {mes}/{anio}', 'success')
        return redirect(url_for('configuracion_tasa'))

    tasas = TasaPeriodo.query.order_by(TasaPeriodo.anio.asc(), TasaPeriodo.mes.asc()).all()

    return render_template(
        'configuracion_tasa.html',
        tasas=tasas
    )


@app.route('/liquidar_credito/<int:credito_id>', methods=['GET', 'POST'])
def liquidar_credito(credito_id):
    if 'user' not in session:
        return redirect('/login')

    credito = Credito.query.get_or_404(credito_id)

    cuotas_activas = Cuota.query.filter(
        Cuota.credito_id == credito.id,
        Cuota.estado.in_(['PENDIENTE', 'EN MORA', 'ABONO'])
    ).order_by(Cuota.numero).all()

    if not cuotas_activas:
        return "Este crédito ya se encuentra liquidado"

    if request.method == 'POST':
        fecha_pago = datetime.strptime(request.form['fecha_pago'], '%Y-%m-%d')
        valor_pago = limpiar_valor_moneda(request.form['valor'])
        medio_pago = request.form['medio_pago']

        if medio_pago == 'OTRO':
            medio_pago_otro = request.form.get('medio_pago_otro', '').strip()
            if not medio_pago_otro:
                return "Debes escribir el otro medio de pago"
            medio_pago = medio_pago_otro

        if valor_pago <= 0:
            return "El valor de la liquidación debe ser mayor que cero"

        # Recalcular mora exacta a la fecha de liquidación
        actualizar_mora_credito(credito, fecha_pago.date())
        db.session.commit()

        cuotas_activas = Cuota.query.filter(
            Cuota.credito_id == credito.id,
            Cuota.estado.in_(['PENDIENTE', 'EN MORA', 'ABONO'])
        ).order_by(Cuota.numero).all()

        if not cuotas_activas:
            return "Este crédito ya se encuentra liquidado"

        cuota_actual = cuotas_activas[0]

        capital_insoluto = round(credito.saldo_actual, 2)
        interes_corriente = round(cuota_actual.interes, 2)
        total_mora = round(sum(round(c.interes_mora or 0, 2) for c in cuotas_activas), 2)

        total_liquidacion = round(capital_insoluto + interes_corriente + total_mora, 2)

        if round(valor_pago, 2) != total_liquidacion:
            return (
                f"El valor ingresado no coincide con la liquidación exacta. "
                f"Debes pagar {total_liquidacion:.2f}"
            )

        # Registrar pago sobre la cuota activa de referencia
        pago = Pago(
            cuota_id=cuota_actual.id,
            fecha=fecha_pago,
            valor=valor_pago,
            medio_pago=medio_pago
        )
        db.session.add(pago)

        # Marcar cuotas
        for i, cuota in enumerate(cuotas_activas):
            cuota.saldo_pendiente = 0
            cuota.dias_mora = 0
            cuota.interes_mora = 0
            cuota.total_cobro = 0

            if i == 0:
                cuota.estado = 'PAGADA'
            else:
                cuota.estado = 'LIQUIDADA'

        # Cerrar crédito
        credito.saldo_actual = 0
        credito.cuota_mensual = 0

        # Solo si ya agregaste estas columnas en models.py
        if hasattr(credito, 'fecha_liquidacion'):
            credito.fecha_liquidacion = fecha_pago
        if hasattr(credito, 'valor_liquidado'):
            credito.valor_liquidado = valor_pago

        db.session.commit()
        return redirect(f'/ver_cuotas/{credito.id}')

    # GET
    actualizar_mora_credito(credito, datetime.utcnow().date())
    db.session.commit()

    cuotas_activas = Cuota.query.filter(
        Cuota.credito_id == credito.id,
        Cuota.estado.in_(['PENDIENTE', 'EN MORA', 'ABONO'])
    ).order_by(Cuota.numero).all()

    if not cuotas_activas:
        return "Este crédito ya se encuentra liquidado"

    cuota_actual = cuotas_activas[0]

    capital_insoluto = round(credito.saldo_actual, 2)
    interes_corriente = round(cuota_actual.interes, 2)
    total_mora = round(sum(round(c.interes_mora or 0, 2) for c in cuotas_activas), 2)
    total_liquidacion = round(capital_insoluto + interes_corriente + total_mora, 2)

    return render_template(
        'liquidar_credito.html',
        credito=credito,
        cuota_actual=cuota_actual,
        capital_insoluto=capital_insoluto,
        interes_corriente=interes_corriente,
        total_mora=total_mora,
        total_liquidacion=total_liquidacion
    )