from app import app
from extensions import db
from models.user import User

with app.app_context():

    users = [
        {"username": "fgipricing", "role": "pricing", "password": "Pricing@fgi123"},
        {"username": "fgicategory", "role": "category", "password": "Category@fgi123"},
        {"username": "fgiadmin", "role": "admin", "password": "Admin@fgi123"},
    ]

    for u in users:
        existing = User.query.filter_by(username=u["username"]).first()

        if not existing:
            user = User(
                username=u["username"],
                role=u["role"]
            )
            user.set_password(u["password"])  # 🔐 hashed here
            db.session.add(user)
        else:
            # Optional: update password if user already exists
            existing.set_password(u["password"])

    db.session.commit()

    print("✅ Users created / updated successfully.")
