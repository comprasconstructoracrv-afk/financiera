from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# 👤 USUARIOS
class Usuario(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(100), nullable=False)
    rol = db.Column(db.String(20), nullable=False)  # admin, cobrador, cliente


# 💰 CRÉDITOS
class Credito(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cliente = db.Column(db.String(100), nullable=False)
    monto = db.Column(db.Float, nullable=False)
    interes = db.Column(db.Float, nullable=False)
    cuotas = db.Column(db.Integer, nullable=False)
    cuota_mensual = db.Column(db.Float)