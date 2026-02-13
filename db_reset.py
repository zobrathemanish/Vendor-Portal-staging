from app import app
from extensions import db

with app.app_context():
    db.drop_all()
    db.create_all()

print("Database reset complete.")
