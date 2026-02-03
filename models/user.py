from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

class User(UserMixin):
    def __init__(self, id, email, password_hash, role, vendor=None):
        self.id = id
        self.email = email
        self.password_hash = password_hash
        self.role = role
        self.vendor = vendor

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
