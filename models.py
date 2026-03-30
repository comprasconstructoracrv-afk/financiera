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
    interes = db.Column(db.Float, nullable=False)
    cuotas = db.Column(db.Integer, nullable=False)
    cuota_mensual = db.Column(db.Float)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

# CUOTAS
class Cuota(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    credito_id = db.Column(db.Integer, db.ForeignKey('credito.id'), nullable=False)
    numero = db.Column(db.Integer, nullable=False)
    fecha_pago = db.Column(db.DateTime, nullable=False)
    valor_cuota = db.Column(db.Float, nullable=False)
    capital = db.Column(db.Float, nullable=False)
    interes = db.Column(db.Float, nullable=False)
    saldo_restante = db.Column(db.Float, nullable=False)
    saldo_pendiente = db.Column(db.Float, nullable=False)
    estado = db.Column(db.String(20), default='PENDIENTE')

# PAGOS
class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cuota_id = db.Column(db.Integer, db.ForeignKey('cuota.id'), nullable=False)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)
    valor = db.Column(db.Float, nullable=False)