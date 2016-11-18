# This is where you can look to get "cheats" about how to code for the bot
# This is intended for Toasty's devs, but you can use it to help fork toasty
---
**How do I restart Toasty:** Either type **python3.5 run.py** or if you want toasty to auto-reboot, **systemctl restart bot.service**





---
## Command Arguments:

**message** - The Message that triggered the command


**channel** - The Channel that the command was triggered in


**Author** - The Member that triggered the command


**server** - The Server that the command was triggered in


**player** - The MusicPlayer (see MusicBot/player.py) associated with the server (if available)


**permissions** - The Permissions that the user has


**user_mentions** - A list of all Members mentioned in the message


**channel_mentions** - A list of all Channels mentioned in the message


**voice_channel** - The voice Channel the bot is in on the server (if available)


**leftover_args** - A list of the arguments given with the command that aren't requiredarguments


**Any argument given to the function that is NOT in the list above will be counted as a REQUIRED positional argument when someone uses the command, thus making the command unusable unless someone uses it perfectly**

The class MusicBot is a subclass of discord.Client so you should use self when using discord.Client functions inside the class in bot.py
Most functions are a coroutine, so you should insert the await keyword before using it
The following utility functions can be used in the MusicBot class (bot.py), which handle and deal with exceptions properly.

```
safe_send_message (in substitute of discord.py's send_message)
safe_edit_message (in substitute of discord.py's edit_message)
safe_print (in substitute of print - handles Unicode issues)
```

---

# Creating commands:
If the command isnt interacting with the player, put it in **misc.py**

If the command creates a playlist, put it in **genre.py**

If the command doesnt do either of those, put it in **bot.py**

---

**Commands must be asynchronous, aka:**
```python
async def cmd_help(self, author, channel):
```
---

**If the command is in misc.py or genre.py**
In bot.py create something to call it:
```python
async def cmd_savage(self, channel, author):
   message = musicbot.misc.savage()
   return Response(message)
```
*and then in misc.py or genre.py*
```python
def savage(): 
    import random
    insults = """[text removed for space]"""
    text = insults.splitlines()
    message = (text[random.randint(0,(len(text))-1)])
    return message
```
