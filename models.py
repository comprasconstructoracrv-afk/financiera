from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

# USUARIOS
class Usuario(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(100), nullable=False)
    rol = db.Column(db.String(20), nullable=False)

# CRÉDITOS
class Credito(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cliente = db.Column(db.String(100), nullable=False)
    monto = db.Column(db.Float, nullable=False)
    abono_inicial = db.Column(db.Float, default=0)
    monto_financiado = db.Column(db.Float, nullable=False)
    saldo_actual = db.Column(db.Float, nullable=False)
    interes = db.Column(db.Float, nullable=False)
    cuotas = db.Column(db.Integer, nullable=False)
    cuota_mensual = db.Column(db.Float)
    tasa_mora_anual = db.Column(db.Float, nullable=False, default=0)
    tasa_mora_mensual = db.Column(db.Float, nullable=False, default=0)
    tasa_mora_diaria = db.Column(db.Float, nullable=False, default=0)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    

# CUOTAS
class Cuota(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    credito_id = db.Column(db.Integer, db.ForeignKey('credito.id'), nullable=False)
    numero = db.Column(db.Integer, nullable=False)
    fecha_pago = db.Column(db.DateTime, nullable=False)
    valor_cuota = db.Column(db.Float, nullable=False)
    saldo_inicial = db.Column(db.Float, nullable=False, default=0)
    capital = db.Column(db.Float, nullable=False)
    interes = db.Column(db.Float, nullable=False)
    saldo_restante = db.Column(db.Float, nullable=False)
    saldo_pendiente = db.Column(db.Float, nullable=False)
    tasa_mora_mensual_cuota = db.Column(db.Float, nullable=False, default=0)
    porcentaje_mora_aplicado = db.Column(db.Float, nullable=False, default=0)
    dias_mora = db.Column(db.Integer, default=0)
    interes_mora = db.Column(db.Float, default=0)
    total_cobro = db.Column(db.Float, default=0)
    estado = db.Column(db.String(20), default='PENDIENTE')

# PAGOS
class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cuota_id = db.Column(db.Integer, db.ForeignKey('cuota.id'), nullable=False)
    fecha = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    valor = db.Column(db.Float, nullable=False)
    medio_pago = db.Column(db.String(50), nullable=False)

class ConfiguracionTasa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(50), unique=True, nullable=False)
    tasa_anual = db.Column(db.Float, nullable=False, default=0)
    tasa_mensual = db.Column(db.Float, nullable=False, default=0)
    tasa_diaria = db.Column(db.Float, nullable=False, default=0)
    fecha_actualizacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class TasaPeriodo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    anio = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)
    tasa_anual = db.Column(db.Float, nullable=False, default=0)
    tasa_mensual = db.Column(db.Float, nullable=False, default=0)
    tasa_diaria = db.Column(db.Float, nullable=False, default=0)