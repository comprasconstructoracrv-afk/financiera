from sqlalchemy import create_engine, text

DATABASE_URL = "postgresql://admin:TMkZiKYYQb9U7G3aVf0KW447c7TcFplF@dpg-d7chbvl7vvec7387m3g0-a.oregon-postgres.render.com/financiero"

engine = create_engine(DATABASE_URL)

with engine.connect() as conn:
    result = conn.execute(text("""
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'credito'::regclass
    """))
    restricciones = [row[0] for row in result]
    print("Restricciones encontradas:", restricciones)

    if "credito_numero_pagare_key" in restricciones:
        conn.execute(text("""
            ALTER TABLE credito
            DROP CONSTRAINT credito_numero_pagare_key
        """))
        conn.commit()
        print("Restricción UNIQUE del pagaré eliminada correctamente.")
    else:
        print("No se encontró la restricción credito_numero_pagare_key.")