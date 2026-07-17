pricate api key: 0c5c283150371e044f207e7c2a9e7bf44cc135d8615dd417115c23bafd9c4ca3

Header Example
Authorization: Bearer 0c5c283150371e044f207e7c2a9e7bf44cc135d8615dd417115c23bafd9c4ca3

HOW TO SEND MESSAGE:
import requests

url = "https://www.wasenderapi.com/api/send-message"
headers = {
    "Authorization": "Bearer YOUR_API_KEY",
    "Content-Type": "application/json"
}

data = {
    "to": "+1234567890",
    "text": "Hello, here is your requested update."
}

response = requests.post(url, json=data, headers=headers)
print(response.json())

BODY RESPONSE OF SENDING MESSAGE RESULT:
{
  "success": true,
  "data": {
    "msgId": 100000,
    "jid": "+123456789",
    "status": "in_progress"
  }
}

HOW TO EDIT MESSAGE
import requests

url = "https://www.wasenderapi.com/api/messages/{msgId}"
headers = {
    "Authorization": "Bearer YOUR_API_KEY",
    "Content-Type": "application/json"
}
data = {"text": "This is the new message content",}
response = requests.put(url, json=data, headers=headers)
print(response.json())

BODY RESPONSE OF MESSAGE EDITING

{
    "success": true,
    "data": {
        "remoteJid": "123456789@s.whatsapp.net",
        "id": "EN82FV0387IVR54JTE2R1",
        "msgId": 100000,
        "key": {
            "id": "EN82FV0387IVR54JTE2R1",
            "fromMe": true,
            "remoteJid": "123456789@s.whatsapp.net"
        },
      "message": {
            "protocolMessage": {
                "key": {
                    "id": "EN82FV0387IVR54JTE2R1",
                    "fromMe": true,
                    "remoteJid": "123456789@s.whatsapp.net"
                },
                "type": 14,
                "timestampMs": 1751302295563,
                "editedMessage": {
                    "extendedTextMessage": {
                        "text": "updated"
                    }
                }
            }
        "messageTimestamp": "1751297488",
        "status": 1
    }
}


HOW TO GET INFO:
import requests

url = "https://www.wasenderapi.com/api/messages/{msgId}/info"
headers = {
    "Authorization": "Bearer YOUR_API_KEY"
}
response = requests.get(url, headers=headers)
print(response.json())

BODY RESPONSE OF INFO:
{
    "success": true,
    "data": {
        "remoteJid": "123456789@s.whatsapp.net",
        "id": "EN82FV0387IVR54JTE2R1",
        "msgId": 100000,
        "key": {
            "id": "EN82FV0387IVR54JTE2R1",
            "fromMe": true,
            "remoteJid": "123456789@s.whatsapp.net"
        },
        "message": {
            "extendedTextMessage": {
                "text": "quoted",
                "contextInfo": {
                    "stanzaId": "SNE5U4M5OSPWHXHN1WBGV",
                    "participant": "123456789@s.whatsapp.net",
                    "quotedMessage": {
                        "extendedTextMessage": {
                            "text": "quoted"
                        }
                    }
                }
            }
        },
        "messageTimestamp": "1751297488",
        "status": 2
    }
}

REQUEST:
import requests

url = "https://www.wasenderapi.com/api/messages/{msgId}"
headers = {
    "Authorization": "Bearer YOUR_API_KEY"
}
response = requests.delete(url, headers=headers)
print(response.json())

RESPONSE:
{
    "success": true,
    "message": "Message deleted successfully."

}

REQUEST TO CHECK IF NUMBER IS IN WHATSAPP
import requests

url = "https://www.wasenderapi.com/api/on-whatsapp/{phone_number}"
headers = {
    "Authorization": "Bearer YOUR_API_KEY"
}
response = requests.get(url, headers=headers)
print(response.json())

RESPONSE RETURNED
{
      "success": true,
      "data": {
        "exists": true,
}

UPLOAD MEDIA FILES
import requests

# Endpoint URL
url = "https://wasenderapi.com/api/upload"

# Path to the local file
file_path = "path/to/your/image.jpg"

# Set the correct MIME type for the file
headers = {
    "Content-Type": "image/jpeg"
}

# Read the file in binary mode and send it in the request body
with open(file_path, "rb") as f:
    response = requests.post(url, headers=headers, data=f)

print(response.json())

RESPONSE SAMPLE
{
  "success": true,
  "publicUrl": "https://wasenderapi.com/media/a1b2c3d4-e5f6-7890-abcd-ef1234567890.jpg"
}

RESEND FAILED MESSAGE REQUEST
import requests

url = "https://www.wasenderapi.com/api/messages/failed-msg-id-123/resend"
headers = {"Authorization": "Bearer YOUR_API_KEY"}

response = requests.post(url, headers=headers)
print(response.json())

RESPONSE SAMPLE
{
    "success": true,
    "message": "Message resend initiated successfully."
}

SEND AUDIO MESSAGE
import requests

url = "https://www.wasenderapi.com/api/send-message"
headers = {
    "Authorization": "Bearer YOUR_API_KEY",
    "Content-Type": "application/json"
}
data = {"to": "+1234567890", "audioUrl": "https: //example.com/announcement.mp3"}
response = requests.post(url, json=data, headers=headers)
print(response.json())

RESPONSE SAMPLE
{
  "success": true,
  "data": {
    "msgId": 100000,
    "jid": "+123456789",
    "status": "in_progress"
  }
}
