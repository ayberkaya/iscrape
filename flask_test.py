from flask import Flask
print(Flask)
app = Flask(__name__)
print(app.before_first_request) 