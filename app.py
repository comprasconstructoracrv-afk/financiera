from flask import Flask, render_template, request, redirect, session, flash, url_for
from models import db, Usuario, Credito, Cuota, Pago, ConfiguracionTasa, TasaPeriodo
from datetime import datetime, date, timedelta
import calendar
import os
from io import BytesIO
from flask import send_file
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

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


def actualizar_mora_credito(credito, fecha_corte=None):
    if isinstance(credito, int):
        credito = Credito.query.get_or_404(credito)

    if fecha_corte is None:
        fecha_corte = date.today()

    cuotas_credito = Cuota.query.filter_by(
        credito_id=credito.id
    ).order_by(Cuota.numero).all()

    for cuota in cuotas_credito:
        fecha_vencimiento = cuota.fecha_pago.date() if isinstance(cuota.fecha_pago, datetime) else cuota.fecha_pago

        if cuota.estado == 'LIQUIDADA':
            cuota.saldo_pendiente = 0
            cuota.dias_mora = 0
            cuota.interes_mora = 0
            cuota.total_cobro = 0
            continue

        saldo_pendiente = round(cuota.saldo_pendiente or 0, 2)
        interes_mora_actual = round(cuota.interes_mora or 0, 2)

        if saldo_pendiente <= 1 and interes_mora_actual <= 1:
            cuota.saldo_pendiente = 0
            cuota.dias_mora = 0
            cuota.interes_mora = 0
            cuota.total_cobro = 0
            cuota.estado = 'PAGADA'
            continue

        saldo_base = saldo_pendiente

        tasa = obtener_tasa_periodo(fecha_vencimiento.year, fecha_vencimiento.month)
        tasa_mensual = tasa.tasa_mensual if tasa else 0
        tasa_diaria = tasa.tasa_diaria if tasa else 0

        cuota.tasa_mora_mensual_cuota = tasa_mensual
        cuota.porcentaje_mora_aplicado = tasa_mensual

        if fecha_corte > fecha_vencimiento and saldo_base > 0:
            dias_mora = (fecha_corte - fecha_vencimiento).days
            interes_mora = saldo_base * tasa_diaria * dias_mora

            cuota.dias_mora = dias_mora
            cuota.interes_mora = round(interes_mora, 2)
            cuota.total_cobro = round(saldo_base + cuota.interes_mora, 2)
            cuota.estado = 'EN MORA'
        else:
            cuota.dias_mora = 0
            cuota.interes_mora = 0
            cuota.total_cobro = round(saldo_base, 2)

            if saldo_base <= 1:
                cuota.saldo_pendiente = 0
                cuota.total_cobro = 0
                cuota.estado = 'PAGADA'
            else:
                cuota.estado = 'PENDIENTE'

def recalcular_cuotas_pendientes(credito, cuota_actual_numero, fecha_base):
    cuotas_futuras = Cuota.query.filter(
        Cuota.credito_id == credito.id,
        Cuota.numero > cuota_actual_numero
    ).order_by(Cuota.numero).all()

    cantidad_cuotas = max(credito.cuotas - cuota_actual_numero, 0)

    if not cuotas_futuras or cantidad_cuotas <= 0:
        return

    # Por seguridad, solo recalculamos la cantidad contractual restante
    cuotas_futuras = cuotas_futuras[:cantidad_cuotas]

    saldo = round(credito.saldo_actual, 2)
    cuota_fija = round(calcular_cuota(saldo, credito.interes, cantidad_cuotas), 2)
    tasa_credito = credito.interes / 100

    config_tasa = ConfiguracionTasa.query.filter_by(nombre='TASA_MORA').first()

    for i, cuota in enumerate(cuotas_futuras):
        saldo_inicial = round(saldo, 2)
        interes_mes = round(saldo_inicial * tasa_credito, 2)
        capital = round(cuota_fija - interes_mes, 2)
        saldo = round(saldo_inicial - capital, 2)

        if saldo < 0:
            capital = round(capital + saldo, 2)
            saldo = 0

        nueva_fecha = sumar_meses(fecha_base, i + 1)

        tasa_periodo = TasaPeriodo.query.filter_by(
            anio=nueva_fecha.year,
            mes=nueva_fecha.month
        ).first()

        cuota.fecha_pago = nueva_fecha
        cuota.saldo_inicial = saldo_inicial
        cuota.valor_cuota = cuota_fija
        cuota.capital = capital
        cuota.interes = interes_mes
        cuota.saldo_restante = saldo
        cuota.saldo_pendiente = cuota_fija
        cuota.dias_mora = 0
        cuota.interes_mora = 0
        cuota.total_cobro = cuota_fija
        cuota.estado = 'PENDIENTE'

        cuota.tasa_mora_mensual_cuota = (
            tasa_periodo.tasa_mensual if tasa_periodo else config_tasa.tasa_mensual
        )
        cuota.porcentaje_mora_aplicado = cuota.tasa_mora_mensual_cuota


def aplicar_pago_deuda_fecha(credito, fecha_pago, valor_pago, medio_pago):
    actualizar_mora_credito(credito, fecha_pago)

    cuotas_exigibles = Cuota.query.filter(
        Cuota.credito_id == credito.id,
        Cuota.estado.in_(['PENDIENTE', 'EN MORA', 'ABONO'])
    ).order_by(Cuota.numero).all()

    cuotas_exigibles = [
        cuota for cuota in cuotas_exigibles
        if (cuota.fecha_pago.date() if isinstance(cuota.fecha_pago, datetime) else cuota.fecha_pago) <= fecha_pago
    ]

    restante = round(valor_pago, 2)
    hubo_abono_extra_capital = False
    ultima_cuota_tocada = None

    for cuota in cuotas_exigibles:
        if restante <= 0:
            break

        ultima_cuota_tocada = cuota

        dias_mora_al_pago = cuota.dias_mora or 0
        mora_generada_al_pago = round(cuota.interes_mora or 0, 2)
        saldo_pendiente_antes_pago = round(cuota.saldo_pendiente or 0, 2)
        total_exigible_al_pago = round((cuota.saldo_pendiente or 0) + (cuota.interes_mora or 0), 2)

        pago = Pago(
            cuota_id=cuota.id,
            fecha=datetime.combine(fecha_pago, datetime.min.time()),
            valor=0,
            medio_pago=medio_pago,
            tipo_pago='PAGO_DEUDA_FECHA',
            dias_mora_pagados=dias_mora_al_pago,
            mora_generada_al_pago=mora_generada_al_pago,
            saldo_pendiente_antes_pago=saldo_pendiente_antes_pago,
            total_exigible_al_pago=total_exigible_al_pago
        )

        valor_aplicado_cuota = 0
        valor_aplicado_mora = 0
        valor_aplicado_interes = 0
        valor_aplicado_capital = 0

        # 1. Cubrir cuota primero
        if cuota.saldo_pendiente > 0 and restante > 0:
            aplicado_cuota = min(restante, round(cuota.saldo_pendiente, 2))
            cuota.saldo_pendiente = round(cuota.saldo_pendiente - aplicado_cuota, 2)
            restante = round(restante - aplicado_cuota, 2)
            valor_aplicado_cuota = round(aplicado_cuota, 2)

            interes_cuota = round(cuota.interes or 0, 2)
            valor_aplicado_interes = min(valor_aplicado_cuota, interes_cuota)
            valor_aplicado_capital = round(max(valor_aplicado_cuota - valor_aplicado_interes, 0), 2)

            if cuota.saldo_pendiente <= 0:
                cuota.saldo_pendiente = 0
                credito.saldo_actual = round(cuota.saldo_restante, 2)

        # 2. Luego cubrir mora
        if cuota.interes_mora > 0 and restante > 0:
            aplicado_mora = min(restante, round(cuota.interes_mora, 2))
            cuota.interes_mora = round(cuota.interes_mora - aplicado_mora, 2)
            restante = round(restante - aplicado_mora, 2)
            valor_aplicado_mora = aplicado_mora

        pago.valor = round(valor_aplicado_cuota + valor_aplicado_mora, 2)
        pago.valor_aplicado_interes = round(valor_aplicado_interes, 2)
        pago.valor_aplicado_capital = round(valor_aplicado_capital, 2)
        pago.valor_aplicado_mora = round(valor_aplicado_mora, 2)
        if pago.valor > 0:
            db.session.add(pago)

        cuota.saldo_pendiente = round(max(cuota.saldo_pendiente, 0), 2)
        cuota.interes_mora = round(max(cuota.interes_mora, 0), 2)

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

    # Solo si ya cubrió toda la deuda exigible a hoy y sobra dinero, va a capital
    if restante > 0:
        credito.saldo_actual = round(credito.saldo_actual - restante, 2)
        if credito.saldo_actual < 0:
            credito.saldo_actual = 0
        hubo_abono_extra_capital = True

    if hubo_abono_extra_capital and ultima_cuota_tocada:
        recalcular_cuotas_pendientes(
            credito=credito,
            cuota_actual_numero=ultima_cuota_tocada.numero,
            fecha_base=ultima_cuota_tocada.fecha_pago
        )

    db.session.commit()



def calcular_componentes_liquidacion(credito, fecha_corte):
    cuotas_activas = Cuota.query.filter(
        Cuota.credito_id == credito.id,
        Cuota.estado.in_(['PENDIENTE', 'EN MORA', 'ABONO'])
    ).order_by(Cuota.numero).all()

    if not cuotas_activas:
        return {
            'cuota_actual': None,
            'capital_insoluto': 0,
            'interes_corriente': 0,
            'total_mora': 0,
            'total_liquidacion': 0
        }

    cuotas_vencidas_o_del_mes = []

    for cuota in cuotas_activas:
        fecha_vencimiento = cuota.fecha_pago.date() if isinstance(cuota.fecha_pago, datetime) else cuota.fecha_pago

        if fecha_vencimiento <= fecha_corte:
            cuotas_vencidas_o_del_mes.append(cuota)

    # Si no hay vencidas todavía, tomar la primera cuota activa como referencia
    if not cuotas_vencidas_o_del_mes:
        cuota_actual = cuotas_activas[0]
        interes_corriente = round(cuota_actual.interes, 2)
        total_mora = round(sum(round(c.interes_mora or 0, 2) for c in cuotas_activas), 2)
    else:
        cuota_actual = cuotas_vencidas_o_del_mes[0]
        interes_corriente = round(sum(round(c.interes, 2) for c in cuotas_vencidas_o_del_mes), 2)
        total_mora = round(sum(round(c.interes_mora or 0, 2) for c in cuotas_vencidas_o_del_mes), 2)

    capital_insoluto = round(credito.saldo_actual, 2)
    total_liquidacion = round(capital_insoluto + interes_corriente + total_mora, 2)

    return {
        'cuota_actual': cuota_actual,
        'capital_insoluto': capital_insoluto,
        'interes_corriente': interes_corriente,
        'total_mora': total_mora,
        'total_liquidacion': total_liquidacion
    }


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

        tasa_periodo = TasaPeriodo.query.filter_by(
            anio=fecha_pago.year,
            mes=fecha_pago.month,
          
        ).first()

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
            tasa_mora_mensual_cuota=tasa_periodo.tasa_mensual if tasa_periodo else config_tasa.tasa_mensual,
            dias_mora=0,
            interes_mora=0,
            total_cobro=cuota_fija,
            estado='PENDIENTE'
        )
        db.session.add(nueva_cuota)

def construir_datos_reporte(anio_seleccionado, sede_seleccionada):
    ...
    return {
        'resumen_general': resumen_general,
        'resumen_por_sede': resumen_por_sede,
        'resumen_mensual': resumen_mensual,
        'labels_sedes': labels_sedes,
        'saldo_actual_sedes': saldo_actual_sedes,
        'interes_causado_sedes': interes_causado_sedes,
        'interes_recaudado_sedes': interes_recaudado_sedes,
        'mora_causada_sedes': mora_causada_sedes,
        'mora_recaudada_sedes': mora_recaudada_sedes,
        'labels_meses': labels_meses,
        'interes_causado_meses': interes_causado_meses,
        'interes_recaudado_meses': interes_recaudado_meses,
        'mora_causada_meses': mora_causada_meses,
        'mora_recaudada_meses': mora_recaudada_meses,
    }


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
        try:
            cliente = request.form['cliente'].strip()
            cedula_cliente = request.form.get('cedula_cliente', '').strip()
            telefono_1 = request.form.get('telefono_1', '').strip()
            telefono_2 = request.form.get('telefono_2', '').strip()
            direccion_cliente = request.form.get('direccion_cliente', '').strip()
            correo_cliente = request.form.get('correo_cliente', '').strip()

            numero_pagare = request.form['numero_pagare'].strip()
            sede = request.form.get('sede', 'CRV')
            monto = limpiar_valor_moneda(request.form['monto'])
            interes = float(request.form['interes'])
            cuotas = int(request.form['cuotas'])
            fecha_credito = datetime.strptime(request.form['fecha_credito'], '%Y-%m-%d')

            tiene_codeudor = request.form.get('tiene_codeudor') == 'SI'

            codeudor_nombre = request.form.get('codeudor_nombre', '').strip() if tiene_codeudor else None
            codeudor_identificacion = request.form.get('codeudor_identificacion', '').strip() if tiene_codeudor else None
            codeudor_direccion = request.form.get('codeudor_direccion', '').strip() if tiene_codeudor else None
            codeudor_telefono = request.form.get('codeudor_telefono', '').strip() if tiene_codeudor else None
            codeudor_correo = request.form.get('codeudor_correo', '').strip() if tiene_codeudor else None

            abono_inicial_texto = request.form.get('abono_inicial', '').strip()
            abono_inicial = limpiar_valor_moneda(abono_inicial_texto) if abono_inicial_texto else 0

            monto_financiado = monto - abono_inicial

            if monto_financiado <= 0:
                flash("El monto financiado debe ser mayor que cero", "error")
                return render_template('crear_credito.html')

            cuota = calcular_cuota(monto_financiado, interes, cuotas)

            config_tasa = ConfiguracionTasa.query.filter_by(nombre='TASA_MORA').first()

            nuevo = Credito(
                numero_pagare=numero_pagare,
                cliente=cliente,
                sede=sede,
                cedula_cliente=cedula_cliente,
                telefono_1=telefono_1,
                telefono_2=telefono_2,
                direccion_cliente=direccion_cliente,
                correo_cliente=correo_cliente,
                tiene_codeudor=tiene_codeudor,
                codeudor_nombre=codeudor_nombre,
                codeudor_identificacion=codeudor_identificacion,
                codeudor_direccion=codeudor_direccion,
                codeudor_telefono=codeudor_telefono,
                codeudor_correo=codeudor_correo,
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

            flash("Crédito creado correctamente", "success")
            return redirect(f'/ver_creditos/{sede}')

        except Exception as e:
            db.session.rollback()
            flash(f"Error al guardar el crédito: {str(e)}", "error")
            return render_template('crear_credito.html')

    return render_template('crear_credito.html')

@app.route('/dashboard')
def dashboard():
    if 'user' not in session:
        return redirect('/login')

    sedes = ['IBAGUE', 'ESPINAL', 'GIRARDOT', 'CRV']
    resumen_sedes = []

    for sede in sedes:
        creditos = Credito.query.filter_by(sede=sede).all()

        total = len(creditos)
        en_mora = 0
        cancelados = 0
        al_dia = 0

        for credito in creditos:
            cuotas = Cuota.query.filter_by(credito_id=credito.id).all()

            if not cuotas:
                continue

            if any(c.estado == 'EN MORA' for c in cuotas):
                en_mora += 1
            elif all(c.estado in ['PAGADA', 'LIQUIDADA'] for c in cuotas):
                cancelados += 1
            else:
                al_dia += 1

        resumen_sedes.append({
            'sede': sede,
            'total': total,
            'en_mora': en_mora,
            'cancelados': cancelados,
            'al_dia': al_dia
        })

    return render_template(
        'dashboard.html',
        resumen_sedes=resumen_sedes
    )


# 🚪 LOGOUT
@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/ver_creditos/<sede>')
def ver_creditos(sede):
    if 'user' not in session:
        return redirect('/login')

    sede = sede.strip().upper()

    creditos = Credito.query.filter_by(sede=sede).order_by(Credito.fecha_creacion.desc()).all()
    hoy = date.today()

    resumen_creditos = []

    for credito in creditos:
        actualizar_mora_credito(credito, hoy)

        cuotas = Cuota.query.filter_by(credito_id=credito.id).all()

        if any(c.estado == 'EN MORA' for c in cuotas):
            estado_credito = 'EN MORA'
        elif all(c.estado in ['PAGADA', 'LIQUIDADA'] for c in cuotas):
            estado_credito = 'CANCELADO'
        else:
            estado_credito = 'AL DÍA'

        resumen_creditos.append({
            'credito': credito,
            'estado_credito': estado_credito
        })

    db.session.commit()

    return render_template(
        'ver_creditos.html',
        resumen_creditos=resumen_creditos,
        sede_actual=sede
    )

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

    hoy = date.today()

    cuotas_exigibles_hoy = []
    for cuota in cuotas:
        fecha_cuota = cuota.fecha_pago.date() if isinstance(cuota.fecha_pago, datetime) else cuota.fecha_pago

        if cuota.estado in ['PENDIENTE', 'EN MORA', 'ABONO'] and fecha_cuota <= hoy:
            cuotas_exigibles_hoy.append(cuota)

    cuota_pendiente_total = round(
        sum((cuota.saldo_pendiente or 0) for cuota in cuotas_exigibles_hoy),
        2
    )
    mora_total = round(
        sum((cuota.interes_mora or 0) for cuota in cuotas_exigibles_hoy),
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

    esta_al_dia = deuda_total_fecha <= 0

    return render_template(
        'ver_cuotas.html',
        credito=credito,
        cuotas=cuotas,
        pagos_por_cuota=pagos_por_cuota,
        ultimo_pago=ultimo_pago,
        estado_credito=estado_credito,
        cuota_pendiente_total=cuota_pendiente_total,
        mora_total=mora_total,
        deuda_total_fecha=deuda_total_fecha,
        esta_al_dia=esta_al_dia,
        cuotas_exigibles_hoy=cuotas_exigibles_hoy
    )


@app.route('/pagar_cuota/<int:cuota_id>', methods=['GET', 'POST'])
def pagar_cuota(cuota_id):
    if 'user' not in session:
        return redirect('/login')

    cuota = Cuota.query.get_or_404(cuota_id)
    credito = Credito.query.get_or_404(cuota.credito_id)

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
            return "El pago debe ser mayor que cero"

        # Recalcular mora exactamente a la fecha del pago
        actualizar_mora_credito(credito, fecha_pago.date())
        db.session.commit()

        # Recargar cuota actualizada
        cuota = Cuota.query.get_or_404(cuota_id)

        # FOTO HISTÓRICA DEL ESTADO DE LA CUOTA AL MOMENTO DEL PAGO
        dias_mora_al_pago = cuota.dias_mora or 0
        mora_generada_al_pago = round(cuota.interes_mora or 0, 2)
        saldo_pendiente_antes_pago = round(cuota.saldo_pendiente or 0, 2)

        restante = round(valor_pago, 2)
        hubo_abono_extra_capital = False

        saldo_cuota_hoy = round(cuota.saldo_pendiente or 0, 2)
        mora_hoy = round(cuota.interes_mora or 0, 2)
        total_exigible = round(saldo_cuota_hoy + mora_hoy, 2)

        valor_aplicado_cuota = 0
        valor_aplicado_mora = 0
        valor_aplicado_prepago = 0
        valor_aplicado_interes = 0
        valor_aplicado_capital = 0

        # 1. Cubrir primero la cuota base
        if cuota.saldo_pendiente > 0:
            aplicado_cuota = min(restante, round(cuota.saldo_pendiente, 2))
            valor_aplicado_cuota = round(valor_aplicado_cuota + aplicado_cuota, 2)
            cuota.saldo_pendiente = round(cuota.saldo_pendiente - aplicado_cuota, 2)
            restante = round(restante - aplicado_cuota, 2)

            # Desglose contable interno de la cuota: primero interés corriente, luego capital
            interes_cuota = round(cuota.interes or 0, 2)
            capital_cuota = round(cuota.capital or 0, 2)

            valor_aplicado_interes = min(valor_aplicado_cuota, interes_cuota)
            valor_aplicado_capital = round(max(valor_aplicado_cuota - valor_aplicado_interes, 0), 2)

            if cuota.saldo_pendiente <= 0:
                cuota.saldo_pendiente = 0
                credito.saldo_actual = round(cuota.saldo_restante, 2)

        # 2. Luego cubrir mora
        if restante > 0 and cuota.interes_mora > 0:
            aplicado_mora = min(restante, round(cuota.interes_mora, 2))
            valor_aplicado_mora = round(valor_aplicado_mora + aplicado_mora, 2)
            cuota.interes_mora = round(cuota.interes_mora - aplicado_mora, 2)
            restante = round(restante - aplicado_mora, 2)

        # 3. Solo es prepago real si pagó más de cuota + mora real de esa fecha
        if restante > 0:
            valor_aplicado_prepago = round(restante, 2)
            credito.saldo_actual = round(credito.saldo_actual - restante, 2)

            if credito.saldo_actual < 0:
                credito.saldo_actual = 0

    # Solo recalcula si el abono es significativo (evita errores por redondeo)
            if valor_aplicado_prepago >= 1:
                hubo_abono_extra_capital = True

            restante = 0

        # Normalización numérica
        cuota.saldo_pendiente = round(max(cuota.saldo_pendiente, 0), 2)
        cuota.interes_mora = round(max(cuota.interes_mora, 0), 2)

        # Limpiar residuos pequeños
        if cuota.saldo_pendiente <= 1:
            cuota.saldo_pendiente = 0

        if cuota.interes_mora <= 1:
            cuota.interes_mora = 0

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

        pago = Pago(
            cuota_id=cuota.id,
            fecha=fecha_pago,
            valor=valor_pago,
            medio_pago=medio_pago,
            valor_aplicado_interes=round(valor_aplicado_interes, 2),
            valor_aplicado_capital=round(valor_aplicado_capital, 2),
            valor_aplicado_mora=round(valor_aplicado_mora, 2),
            valor_aplicado_prepago_capital=round(valor_aplicado_prepago, 2),
            tipo_pago='PAGO_CUOTA',
            dias_mora_pagados=dias_mora_al_pago,
            mora_generada_al_pago=mora_generada_al_pago,
            saldo_pendiente_antes_pago=saldo_pendiente_antes_pago,
            total_exigible_al_pago=total_exigible
        )
        db.session.add(pago)

        db.session.commit()
        return redirect(f'/ver_cuotas/{cuota.credito_id}')

    actualizar_mora_credito(credito, datetime.utcnow().date())
    db.session.commit()
    cuota = Cuota.query.get_or_404(cuota_id)

    return render_template('pagar_cuota.html', cuota=cuota)


@app.route('/pagar_deuda_fecha/<int:credito_id>', methods=['GET', 'POST'])
def pagar_deuda_fecha(credito_id):
    if 'user' not in session:
        return redirect('/login')

    credito = Credito.query.get_or_404(credito_id)

    if request.method == 'POST':
        fecha_pago = datetime.strptime(request.form['fecha_pago'], '%Y-%m-%d').date()
        valor_pago = limpiar_valor_moneda(request.form['valor'])
        medio_pago = request.form['medio_pago']

        if medio_pago == 'OTRO':
            medio_pago_otro = request.form.get('medio_pago_otro', '').strip()
            if not medio_pago_otro:
                return "Debes escribir el otro medio de pago"
            medio_pago = medio_pago_otro

        if valor_pago <= 0:
            return "El pago debe ser mayor que cero"

        aplicar_pago_deuda_fecha(
            credito=credito,
            fecha_pago=fecha_pago,
            valor_pago=valor_pago,
            medio_pago=medio_pago
        )

        return redirect(f'/ver_cuotas/{credito.id}')

    actualizar_mora_credito(credito, datetime.utcnow().date())
    db.session.commit()

    cuotas = Cuota.query.filter(
        Cuota.credito_id == credito.id,
        Cuota.estado.in_(['PENDIENTE', 'EN MORA', 'ABONO'])
    ).order_by(Cuota.numero).all()

    hoy = datetime.utcnow().date()
    cuotas_exigibles_hoy = [
        cuota for cuota in cuotas
        if (cuota.fecha_pago.date() if isinstance(cuota.fecha_pago, datetime) else cuota.fecha_pago) <= hoy
    ]

    cuota_pendiente_total = round(sum((c.saldo_pendiente or 0) for c in cuotas_exigibles_hoy), 2)
    mora_total = round(sum((c.interes_mora or 0) for c in cuotas_exigibles_hoy), 2)
    deuda_total_fecha = round(cuota_pendiente_total + mora_total, 2)

    return render_template(
        'pagar_deuda_fecha.html',
        credito=credito,
        cuotas_exigibles_hoy=cuotas_exigibles_hoy,
        cuota_pendiente_total=cuota_pendiente_total,
        mora_total=mora_total,
        deuda_total_fecha=deuda_total_fecha
    )


@app.route('/abono_capital/<int:credito_id>', methods=['GET', 'POST'])
def abono_capital(credito_id):
    if 'user' not in session:
        return redirect('/login')

    credito = Credito.query.get_or_404(credito_id)

    actualizar_mora_credito(credito, datetime.utcnow().date())
    db.session.commit()

    cuotas_activas = Cuota.query.filter(
        Cuota.credito_id == credito.id,
        Cuota.estado.in_(['PENDIENTE', 'EN MORA', 'ABONO'])
    ).order_by(Cuota.numero).all()

    hoy = datetime.utcnow().date()
    cuotas_exigibles_hoy = [
        cuota for cuota in cuotas_activas
        if (cuota.fecha_pago.date() if isinstance(cuota.fecha_pago, datetime) else cuota.fecha_pago) <= hoy
    ]

    deuda_total_fecha = round(
        sum((cuota.saldo_pendiente or 0) + (cuota.interes_mora or 0) for cuota in cuotas_exigibles_hoy),
        2
    )

    if request.method == 'POST':
        fecha_pago = datetime.strptime(request.form['fecha_pago'], '%Y-%m-%d')
        valor_pago = limpiar_valor_moneda(request.form['valor'])
        medio_pago = request.form['medio_pago']

        if medio_pago == 'OTRO':
            medio_pago_otro = request.form.get('medio_pago_otro', '').strip()
            if not medio_pago_otro:
                return "Debes escribir el otro medio de pago"
            medio_pago = medio_pago_otro

        if deuda_total_fecha > 0:
            return "No se puede hacer abono a capital mientras existan cuotas exigibles o mora pendiente"

        if valor_pago <= 0:
            return "El abono a capital debe ser mayor que cero"

        if valor_pago > credito.saldo_actual:
            return "El abono a capital no puede ser mayor al saldo actual del crédito"

        credito.saldo_actual = round(credito.saldo_actual - valor_pago, 2)

        if credito.saldo_actual < 0:
            credito.saldo_actual = 0

        cuota_referencia = Cuota.query.filter(
            Cuota.credito_id == credito.id,
            Cuota.estado == 'PENDIENTE'
        ).order_by(Cuota.numero).first()

        if cuota_referencia:
            recalcular_cuotas_pendientes(
                credito=credito,
                cuota_actual_numero=cuota_referencia.numero - 1,
                fecha_base=sumar_meses(cuota_referencia.fecha_pago, -1)
            )

            pago = Pago(
                cuota_id=cuota_referencia.id,
                fecha=fecha_pago,
                valor=valor_pago,
                medio_pago=medio_pago,
                valor_aplicado_prepago_capital=valor_pago,
                observacion='ABONO A CAPITAL'
            )
            db.session.add(pago)

        db.session.commit()
        return redirect(f'/ver_cuotas/{credito.id}')

    return render_template(
        'abono_capital.html',
        credito=credito,
        deuda_total_fecha=deuda_total_fecha
    )


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

        actualizar_mora_credito(credito, fecha_pago.date())
        db.session.commit()

        cuotas_activas = Cuota.query.filter(
            Cuota.credito_id == credito.id,
            Cuota.estado.in_(['PENDIENTE', 'EN MORA', 'ABONO'])
        ).order_by(Cuota.numero).all()

        if not cuotas_activas:
            return "Este crédito ya se encuentra liquidado"

        componentes = calcular_componentes_liquidacion(credito, fecha_pago.date())

        cuota_actual = componentes['cuota_actual']
        capital_insoluto = componentes['capital_insoluto']
        interes_corriente = componentes['interes_corriente']
        total_mora = componentes['total_mora']
        total_liquidacion = componentes['total_liquidacion']

        if round(valor_pago) != round(total_liquidacion):
            return (
                f"El valor ingresado no coincide con la liquidación exacta. "
                f"Debes pagar {round(total_liquidacion)}"
            )

        pago = Pago(
            cuota_id=cuota_actual.id,
            fecha=fecha_pago,
            valor=valor_pago,
            medio_pago=medio_pago,
            valor_aplicado_mora=total_mora,
            valor_aplicado_capital=0,
            valor_aplicado_prepago_capital=round(valor_pago - total_mora, 2),
            tipo_pago='LIQUIDACION_TOTAL',
            dias_mora_pagados=cuota_actual.dias_mora or 0,
            mora_generada_al_pago=total_mora,
            saldo_pendiente_antes_pago=capital_insoluto,
            total_exigible_al_pago=total_liquidacion,
            observacion='LIQUIDACION TOTAL DEL CREDITO'
        )
        db.session.add(pago)

        for i, cuota in enumerate(cuotas_activas):
            cuota.saldo_pendiente = 0
            cuota.dias_mora = 0
            cuota.interes_mora = 0
            cuota.total_cobro = 0

            if i == 0:
                cuota.estado = 'PAGADA'
            else:
                cuota.estado = 'LIQUIDADA'

        credito.saldo_actual = 0
        credito.cuota_mensual = 0

        if hasattr(credito, 'fecha_liquidacion'):
            credito.fecha_liquidacion = fecha_pago
        if hasattr(credito, 'valor_liquidado'):
            credito.valor_liquidado = valor_pago

        db.session.commit()
        return redirect(f'/ver_cuotas/{credito.id}')

    actualizar_mora_credito(credito, datetime.utcnow().date())
    db.session.commit()

    cuotas_activas = Cuota.query.filter(
        Cuota.credito_id == credito.id,
        Cuota.estado.in_(['PENDIENTE', 'EN MORA', 'ABONO'])
    ).order_by(Cuota.numero).all()

    if not cuotas_activas:
        return "Este crédito ya se encuentra liquidado"

    componentes = calcular_componentes_liquidacion(credito, datetime.utcnow().date())

    cuota_actual = componentes['cuota_actual']
    capital_insoluto = componentes['capital_insoluto']
    interes_corriente = componentes['interes_corriente']
    total_mora = componentes['total_mora']
    total_liquidacion = componentes['total_liquidacion']

    return render_template(
        'liquidar_credito.html',
        credito=credito,
        cuota_actual=cuota_actual,
        capital_insoluto=capital_insoluto,
        interes_corriente=interes_corriente,
        total_mora=total_mora,
        total_liquidacion=total_liquidacion
    )

def construir_datos_reporte(anio_seleccionado, sede_seleccionada):
    sedes = ['IBAGUE', 'ESPINAL', 'GIRARDOT', 'CRV']

    meses_nombres = {
        1: 'ENERO',
        2: 'FEBRERO',
        3: 'MARZO',
        4: 'ABRIL',
        5: 'MAYO',
        6: 'JUNIO',
        7: 'JULIO',
        8: 'AGOSTO',
        9: 'SEPTIEMBRE',
        10: 'OCTUBRE',
        11: 'NOVIEMBRE',
        12: 'DICIEMBRE'
    }

    if sede_seleccionada == 'TODAS':
        sedes_filtradas = sedes
    else:
        sedes_filtradas = [sede_seleccionada]

    resumen_general = {
        'total_prestado': 0,
        'total_recaudado': 0,
        'saldo_actual_total': 0,
        'interes_corriente_causado': 0,
        'interes_corriente_recaudado': 0,
        'mora_causada': 0,
        'mora_recaudada': 0
    }

    resumen_por_sede = []

    for sede in sedes_filtradas:
        creditos = Credito.query.filter(
            Credito.sede == sede,
            db.extract('year', Credito.fecha_creacion) == anio_seleccionado
        ).all()

        creditos_ids = [c.id for c in creditos]

        total_prestado = round(sum(c.monto_financiado or 0 for c in creditos), 2)
        saldo_actual = round(sum(c.saldo_actual or 0 for c in creditos), 2)

        interes_corriente_causado = 0
        mora_causada = 0
        interes_corriente_recaudado = 0
        mora_recaudada = 0
        total_recaudado = 0

        if creditos_ids:
            cuotas = Cuota.query.filter(Cuota.credito_id.in_(creditos_ids)).all()
            cuotas_ids = [cuota.id for cuota in cuotas]

            interes_corriente_causado = round(sum(cuota.interes or 0 for cuota in cuotas), 2)
            mora_causada = round(sum((cuota.interes_mora_historico or 0) + (cuota.interes_mora or 0) for cuota in cuotas), 2)

            if cuotas_ids:
                pagos = Pago.query.filter(Pago.cuota_id.in_(cuotas_ids)).all()
                total_recaudado = round(sum(pago.valor or 0 for pago in pagos), 2)
                interes_corriente_recaudado = round(sum(pago.valor_aplicado_interes or 0 for pago in pagos), 2)
                mora_recaudada = round(sum(pago.valor_aplicado_mora or 0 for pago in pagos), 2)

        resumen_general['total_prestado'] += total_prestado
        resumen_general['total_recaudado'] += total_recaudado
        resumen_general['saldo_actual_total'] += saldo_actual
        resumen_general['interes_corriente_causado'] += interes_corriente_causado
        resumen_general['interes_corriente_recaudado'] += interes_corriente_recaudado
        resumen_general['mora_causada'] += mora_causada
        resumen_general['mora_recaudada'] += mora_recaudada

        resumen_por_sede.append({
            'sede': sede,
            'total_prestado': round(total_prestado, 2),
            'total_recaudado': round(total_recaudado, 2),
            'saldo_actual': round(saldo_actual, 2),
            'interes_corriente_causado': round(interes_corriente_causado, 2),
            'interes_corriente_recaudado': round(interes_corriente_recaudado, 2),
            'mora_causada': round(mora_causada, 2),
            'mora_recaudada': round(mora_recaudada, 2)
        })

    resumen_mensual = []

    for mes in range(1, 13):
        pagos_mes = Pago.query.filter(
            db.extract('year', Pago.fecha) == anio_seleccionado,
            db.extract('month', Pago.fecha) == mes
        ).all()

        total_ingresos_mes = round(sum(pago.valor or 0 for pago in pagos_mes), 2)
        interes_corriente_recaudado_mes = round(sum(pago.valor_aplicado_interes or 0 for pago in pagos_mes), 2)
        mora_recaudada_mes = round(sum(pago.valor_aplicado_mora or 0 for pago in pagos_mes), 2)

        cuotas_mes = Cuota.query.filter(
            db.extract('year', Cuota.fecha_pago) == anio_seleccionado,
            db.extract('month', Cuota.fecha_pago) == mes
        ).all()

        interes_corriente_causado_mes = round(sum(cuota.interes or 0 for cuota in cuotas_mes), 2)
        mora_causada_mes = round(sum((cuota.interes_mora_historico or 0) + (cuota.interes_mora or 0) for cuota in cuotas_mes), 2)

        resumen_mensual.append({
            'mes': meses_nombres[mes],
            'interes_corriente_causado': interes_corriente_causado_mes,
            'interes_corriente_recaudado': interes_corriente_recaudado_mes,
            'mora_causada': mora_causada_mes,
            'mora_recaudada': mora_recaudada_mes,
            'total_ingresos': total_ingresos_mes
        })

    for clave in resumen_general:
        resumen_general[clave] = round(resumen_general[clave], 2)

    labels_sedes = [fila['sede'] for fila in resumen_por_sede]
    saldo_actual_sedes = [fila['saldo_actual'] for fila in resumen_por_sede]
    interes_causado_sedes = [fila['interes_corriente_causado'] for fila in resumen_por_sede]
    interes_recaudado_sedes = [fila['interes_corriente_recaudado'] for fila in resumen_por_sede]
    mora_causada_sedes = [fila['mora_causada'] for fila in resumen_por_sede]
    mora_recaudada_sedes = [fila['mora_recaudada'] for fila in resumen_por_sede]

    labels_meses = [fila['mes'] for fila in resumen_mensual]
    interes_causado_meses = [fila['interes_corriente_causado'] for fila in resumen_mensual]
    interes_recaudado_meses = [fila['interes_corriente_recaudado'] for fila in resumen_mensual]
    mora_causada_meses = [fila['mora_causada'] for fila in resumen_mensual]
    mora_recaudada_meses = [fila['mora_recaudada'] for fila in resumen_mensual]

    return {
        'resumen_general': resumen_general,
        'resumen_por_sede': resumen_por_sede,
        'resumen_mensual': resumen_mensual,
        'labels_sedes': labels_sedes,
        'saldo_actual_sedes': saldo_actual_sedes,
        'interes_causado_sedes': interes_causado_sedes,
        'interes_recaudado_sedes': interes_recaudado_sedes,
        'mora_causada_sedes': mora_causada_sedes,
        'mora_recaudada_sedes': mora_recaudada_sedes,
        'labels_meses': labels_meses,
        'interes_causado_meses': interes_causado_meses,
        'interes_recaudado_meses': interes_recaudado_meses,
        'mora_causada_meses': mora_causada_meses,
        'mora_recaudada_meses': mora_recaudada_meses
    }

@app.route('/reporte_financiero')
def reporte_financiero():
    if 'user' not in session:
        return redirect('/login')

    anio_actual = date.today().year
    anio_seleccionado = request.args.get('anio', type=int) or anio_actual
    sede_seleccionada = request.args.get('sede', default='TODAS', type=str).strip().upper()

    datos = construir_datos_reporte(anio_seleccionado, sede_seleccionada)

    return render_template(
        'reporte_financiero.html',
        anio_actual=anio_actual,
        anio_seleccionado=anio_seleccionado,
        anios_disponibles=list(range(2024, anio_actual + 2)),
        sede_seleccionada=sede_seleccionada,
        sedes_disponibles=['TODAS', 'IBAGUE', 'ESPINAL', 'GIRARDOT', 'CRV'],
        **datos
    )

@app.route('/reporte_financiero/excel')
def exportar_reporte_excel():
    if 'user' not in session:
        return redirect('/login')

    anio_actual = date.today().year
    anio_seleccionado = request.args.get('anio', type=int) or anio_actual
    sede_seleccionada = request.args.get('sede', default='TODAS', type=str).strip().upper()

    datos = construir_datos_reporte(anio_seleccionado, sede_seleccionada)

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Resumen General"

    # Estilos básicos
    fill_header = PatternFill("solid", fgColor="1F4E78")
    fill_sub = PatternFill("solid", fgColor="D9EAF7")
    font_white = Font(color="FFFFFF", bold=True)
    font_bold = Font(bold=True)
    center = Alignment(horizontal="center", vertical="center")

    # Título
    ws1["A1"] = f"Reporte Financiero - Año {anio_seleccionado} - Sede {sede_seleccionada}"
    ws1["A1"].font = Font(bold=True, size=14)

    # Resumen general
    ws1["A3"] = "Concepto"
    ws1["B3"] = "Valor"
    for c in ["A3", "B3"]:
        ws1[c].fill = fill_header
        ws1[c].font = font_white
        ws1[c].alignment = center

    resumen_general = datos["resumen_general"]
    filas_general = [
        ("Total prestado", resumen_general["total_prestado"]),
        ("Total recaudado", resumen_general["total_recaudado"]),
        ("Saldo actual total", resumen_general["saldo_actual_total"]),
        ("Interés corriente causado", resumen_general["interes_corriente_causado"]),
        ("Interés corriente recaudado", resumen_general["interes_corriente_recaudado"]),
        ("Mora causada", resumen_general["mora_causada"]),
        ("Mora recaudada", resumen_general["mora_recaudada"]),
    ]

    fila = 4
    for concepto, valor in filas_general:
        ws1[f"A{fila}"] = concepto
        ws1[f"B{fila}"] = valor
        ws1[f"B{fila}"].number_format = '$ #,##0'
        fila += 1

    # Hoja por sede
    ws2 = wb.create_sheet("Por Sede")
    headers_sede = [
        "Sede", "Total prestado", "Total recaudado", "Saldo actual",
        "Interés corriente causado", "Interés corriente recaudado",
        "Mora causada", "Mora recaudada"
    ]
    ws2.append(headers_sede)
    for col in range(1, len(headers_sede) + 1):
        cell = ws2.cell(row=1, column=col)
        cell.fill = fill_header
        cell.font = font_white
        cell.alignment = center

    for item in datos["resumen_por_sede"]:
        ws2.append([
            item["sede"],
            item["total_prestado"],
            item["total_recaudado"],
            item["saldo_actual"],
            item["interes_corriente_causado"],
            item["interes_corriente_recaudado"],
            item["mora_causada"],
            item["mora_recaudada"],
        ])

    for row in ws2.iter_rows(min_row=2, min_col=2, max_col=8):
        for cell in row:
            cell.number_format = '$ #,##0'

    # Hoja mensual
    ws3 = wb.create_sheet("Resumen Mensual")
    headers_mes = [
        "Mes", "Interés corriente causado", "Interés corriente recaudado",
        "Mora causada", "Mora recaudada", "Total ingresos"
    ]
    ws3.append(headers_mes)
    for col in range(1, len(headers_mes) + 1):
        cell = ws3.cell(row=1, column=col)
        cell.fill = fill_header
        cell.font = font_white
        cell.alignment = center

    for item in datos["resumen_mensual"]:
        ws3.append([
            item["mes"],
            item["interes_corriente_causado"],
            item["interes_corriente_recaudado"],
            item["mora_causada"],
            item["mora_recaudada"],
            item["total_ingresos"],
        ])

    for row in ws3.iter_rows(min_row=2, min_col=2, max_col=6):
        for cell in row:
            cell.number_format = '$ #,##0'

    # Anchos
    for ws in [ws1, ws2, ws3]:
        for col in ws.columns:
            max_length = 0
            col_letter = col[0].column_letter
            for cell in col:
                try:
                    max_length = max(max_length, len(str(cell.value)))
                except:
                    pass
            ws.column_dimensions[col_letter].width = min(max_length + 3, 28)

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    nombre = f"reporte_financiero_{anio_seleccionado}_{sede_seleccionada}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=nombre,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@app.route('/reporte_financiero/pdf')
def exportar_reporte_pdf():
    if 'user' not in session:
        return redirect('/login')

    anio_actual = date.today().year
    anio_seleccionado = request.args.get('anio', type=int) or anio_actual
    sede_seleccionada = request.args.get('sede', default='TODAS', type=str).strip().upper()

    datos = construir_datos_reporte(anio_seleccionado, sede_seleccionada)

    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=landscape(letter))
    styles = getSampleStyleSheet()
    elementos = []

    elementos.append(Paragraph(f"Reporte Financiero - Año {anio_seleccionado} - Sede {sede_seleccionada}", styles["Title"]))
    elementos.append(Spacer(1, 12))

    # Resumen general
    elementos.append(Paragraph("Resumen General", styles["Heading2"]))
    tabla_general = [
        ["Concepto", "Valor"],
        ["Total prestado", f"$ {int(round(datos['resumen_general']['total_prestado'])):,}".replace(",", ".")],
        ["Total recaudado", f"$ {int(round(datos['resumen_general']['total_recaudado'])):,}".replace(",", ".")],
        ["Saldo actual total", f"$ {int(round(datos['resumen_general']['saldo_actual_total'])):,}".replace(",", ".")],
        ["Interés corriente causado", f"$ {int(round(datos['resumen_general']['interes_corriente_causado'])):,}".replace(",", ".")],
        ["Interés corriente recaudado", f"$ {int(round(datos['resumen_general']['interes_corriente_recaudado'])):,}".replace(",", ".")],
        ["Mora causada", f"$ {int(round(datos['resumen_general']['mora_causada'])):,}".replace(",", ".")],
        ["Mora recaudada", f"$ {int(round(datos['resumen_general']['mora_recaudada'])):,}".replace(",", ".")],
    ]
    t1 = Table(tabla_general, repeatRows=1)
    t1.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1F4E78')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('BACKGROUND', (0,1), (-1,-1), colors.whitesmoke),
    ]))
    elementos.append(t1)
    elementos.append(Spacer(1, 18))

    # Resumen por sede
    elementos.append(Paragraph("Resumen por Sede", styles["Heading2"]))
    tabla_sede = [[
        "Sede", "Total prestado", "Total recaudado", "Saldo actual",
        "Interés causado", "Interés recaudado", "Mora causada", "Mora recaudada"
    ]]
    for fila in datos["resumen_por_sede"]:
        tabla_sede.append([
            fila["sede"],
            f"$ {int(round(fila['total_prestado'])):,}".replace(",", "."),
            f"$ {int(round(fila['total_recaudado'])):,}".replace(",", "."),
            f"$ {int(round(fila['saldo_actual'])):,}".replace(",", "."),
            f"$ {int(round(fila['interes_corriente_causado'])):,}".replace(",", "."),
            f"$ {int(round(fila['interes_corriente_recaudado'])):,}".replace(",", "."),
            f"$ {int(round(fila['mora_causada'])):,}".replace(",", "."),
            f"$ {int(round(fila['mora_recaudada'])):,}".replace(",", "."),
        ])
    t2 = Table(tabla_sede, repeatRows=1)
    t2.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1F4E78')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('BACKGROUND', (0,1), (-1,-1), colors.beige),
    ]))
    elementos.append(t2)
    elementos.append(Spacer(1, 18))

    # Resumen mensual
    elementos.append(Paragraph("Resumen Mensual", styles["Heading2"]))
    tabla_mes = [[
        "Mes", "Interés causado", "Interés recaudado",
        "Mora causada", "Mora recaudada", "Total ingresos"
    ]]
    for fila in datos["resumen_mensual"]:
        tabla_mes.append([
            fila["mes"],
            f"$ {int(round(fila['interes_corriente_causado'])):,}".replace(",", "."),
            f"$ {int(round(fila['interes_corriente_recaudado'])):,}".replace(",", "."),
            f"$ {int(round(fila['mora_causada'])):,}".replace(",", "."),
            f"$ {int(round(fila['mora_recaudada'])):,}".replace(",", "."),
            f"$ {int(round(fila['total_ingresos'])):,}".replace(",", "."),
        ])
    t3 = Table(tabla_mes, repeatRows=1)
    t3.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1F4E78')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('BACKGROUND', (0,1), (-1,-1), colors.whitesmoke),
    ]))
    elementos.append(t3)

    doc.build(elementos)
    output.seek(0)

    nombre = f"reporte_financiero_{anio_seleccionado}_{sede_seleccionada}.pdf"
    return send_file(
        output,
        as_attachment=True,
        download_name=nombre,
        mimetype='application/pdf'
    )

if __name__ == "__main__":
    app.run(debug=True)