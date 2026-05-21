import requests

class AkeneoClient:
    def __init__(self, base_url, client_id, client_secret, username, password):
        self.base_url = base_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.username = username
        self.password = password
        self.token = self.get_token()

    def get_token(self):
        url = f"{self.base_url}/api/oauth/v1/token"

        response = requests.post(
            url,
            data={
                "grant_type": "password",
                "username": self.username,
                "password": self.password
            },
            auth=(self.client_id, self.client_secret)
        )

        return response.json()["access_token"]

    def headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }