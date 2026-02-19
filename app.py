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
from routes.category_review_routes import category_review_bp



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
app.register_blueprint(category_review_bp)



# ---------------------------------------
# MAIN
# ---------------------------------------

if __name__ == "__main__":
    logger.info("Vendor Portal started")
    app.run(debug=True)

'''
We have only the delta rows in category UI. A single vendor can have multiple rows some with insert, update, or delete delta type 
(change type). This is the point where we promote data to silver/approved. 
- We need to take the new etl_mapped and wait for the category team to use the UI:
- Delta type insert: If they hit 'approve' on UI, there's nothing we need to do. 
- Delta type insert: If they hit 'disapprove' on UI, we need to delete that from our etl_mapped.xlsx and etl_mapped.parquet
- It could happen multiple times until all is completed.
- Delta type update: Approve, no changes to etl mapped
- Delta type update: Disapprove, (fetch etl_mapped from current_state and the old row should be in new etl_mapped, but also update the 
product lifestatus to obsolete.)
- Delta type delete: Do not show the approve/Disapprove/Hold button for this. 

- After all the rows in insert, update is approved/disapproved.. replace the latest etl_mapped to current_state and move it to gold/selected

Or:
INSERT
    Approve → Add
    Reject  → Ignore

UPDATE
    Approve → Replace
    Reject  → Mark inactive

DELETE
    → Mark inactive


'''