class SimpleUser:
    def __init__(self, id, email, role, vendor=None):
        self.id = id
        self.email = email
        self.role = role
        self.vendor = vendor

    def is_authenticated(self):
        return True

    def is_active(self):
        return True

    def is_anonymous(self):
        return False

    def get_id(self):
        return str(self.id)


USERS = {
    "vendor@grote.com": {
        "id": 1,
        "password": "vendor123",
        "role": "vendor",
        "vendor": "Grote Lighting"
    },
    "admin@fgi.com": {
        "id": 2,
        "password": "admin123",
        "role": "admin",
        "vendor": None
    }
}
