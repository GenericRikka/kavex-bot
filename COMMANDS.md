# 🤖Discord Bot Commands

```
/command_channel #channel
```
This command limits bot commands to the specified channel, the bot will refuse to process commands originating from another channel.

---

```
/level @user
```
Show the current level (messaging level) of @user, if no user is specified the user executing the command is used.

---

```
/set_member_role @role
```
Sets the default role assigned to new users as they join the server to @role

---

```
/member_role
```
Displays the role that is currently handed out to new users by default.

---

```
/minecraft connect [TOKEN] #channel
```
Connects the minecraft server with the KavexLink plugin token [TOKEN] to #channel
Ingame chat messages will then be synced with that channel, when a connection could be established.

---

```
/minecraft debug_hash
```
Prints the computed token hash from the database for troubleshooting.

---

```
/minecraft debug_links
```
Prints stored linkes between hashed token and #channel for this guild from the database for troubleshooting.

---

```
/minecraft disconnect #channel
```
Deletes the link between hashed plugin token and #channel for #channel from the database.

---

```
/minecraft status #channel
```
Shows the links connection status for #channel

---

```
/minecraft test_send #channel
```
Tries to send a test message into #channel via webhook. For permission troubleshooting.

---

```
/rr add [messageID/link] [emoji] @role
```
Adds a rection with [emoji] to [messageID] and monitors for other reactions. If someone reacts with that emoji to the message, they will be assigned @role, if they remove their reaction again @role is aswell removed from them.
 > Note: The bot's highes role needs to be above the roles it is handing out.

---

```
/rr remove [messageID/link] [emoji]
```
Removes the bot's reaction with [emoji] to [messageID] and removes the reaction based role assignement for [emoji] reactions on [messageID].

---

```
/welcome_set_channel #channel
```
Sets the channel in which to send welcome messages to new users.

---

```
/welcome_use_message [messageID/link]
```
Uses [messageID] as template for the welcome message. Variables like 'new_user' and 'server_name' will be filled out with a ping to the new user joining and the guilds name the bot is on.
