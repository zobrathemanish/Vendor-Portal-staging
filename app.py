from flask import Flask
from dotenv import load_dotenv
import os

from config import Config
from extensions import logger, login_manager, db

# Blueprints
from routes.auth_routes import auth_bp
from routes.upload_routes import upload_bp
from routes.api_routes import api_bp
from routes.single_product_routes import single_product_bp
from routes.utility_routes import utility_bp


load_dotenv()

# ---------------------------------------
# APP INIT
# ---------------------------------------

app = Flask(__name__)
app.config.from_object(Config)

# ---------------------------------------
# DATABASE CONFIG
# ---------------------------------------

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///users.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
import os
print("DB FILE LOCATION:", os.path.abspath("users.db"))


db.init_app(app)


# ---------------------------------------
# LOGIN MANAGER
# ---------------------------------------

login_manager.login_view = "auth.login"
login_manager.init_app(app)

# ---------------------------------------
# Ensure upload folder exists
# ---------------------------------------

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

# ---------------------------------------
# REGISTER BLUEPRINTS
# ---------------------------------------

app.register_blueprint(auth_bp)
app.register_blueprint(upload_bp)
app.register_blueprint(api_bp)
app.register_blueprint(single_product_bp)
app.register_blueprint(utility_bp)



# ---------------------------------------
# MAIN
# ---------------------------------------

if __name__ == "__main__":
    logger.info("Vendor Portal started")
    app.run(debug=True)
