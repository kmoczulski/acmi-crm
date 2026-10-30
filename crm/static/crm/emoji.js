// The emoji Chat's picker offers (static/crm/chat.js): a reaction, or a smiley in a message. Each entry is the emoji, a space,
// then the words it is found by. Emoji that need the "show as a picture" mark get it here (a bare ❤ shows as text).
(function () {
  const NEEDS_MARK = new Set('☹☠❣❤✌☝✍🖐👁🕵🕳☀☁⛈🌧❄⌨🖥🖨🖱☎⚖⚙✂✏🖊🖇✉🏷🏗✔✖⚠ℹ‼⁉▶⏸⏹⏭⏮♻©®✈'.split(''));
  const fix = e => [...e].map((c, i, all) => (NEEDS_MARK.has(c) && all[i + 1] !== '️' ? c + '️' : c)).join('');
  const group = (name, icon, text) => ({
    name, icon: fix(icon),
    items: text.trim().split(/\s*\n\s*/).map(line => {
      const i = line.indexOf(' ');
      return [fix(line.slice(0, i)), line.slice(i + 1)];
    }),
  });

  window.ACMI_EMOJI = {
    quick: ['👍', '❤', '😂', '😮', '😢', '🙏'].map(fix),
    groups: [
      group('Smileys', '😀', `
😀 grinning face happy smile
😃 smiley happy joy
😄 smile happy laugh
😁 grin beaming
😆 laughing squint
😅 sweat smile relief
😂 joy tears laugh lol funny
🤣 rofl rolling laughing
🙂 slight smile
🙃 upside down
😉 wink
😊 blush happy smile
😇 angel halo innocent
🥰 love hearts adore
😍 heart eyes love
🤩 star struck wow
😘 kiss blow
😗 kissing
😋 yum tasty delicious
😛 tongue
😜 wink tongue silly
🤪 zany crazy
🤑 money mouth rich
🤗 hug hugging
🤭 oops giggle hand over mouth
🤫 shush quiet secret
🤔 thinking hmm think
🤐 zipper mouth secret
🤨 raised eyebrow skeptical
😐 neutral meh
😑 expressionless
😶 no mouth silent
😏 smirk
😒 unamused
🙄 eye roll
😬 grimace awkward
😌 relieved calm
😔 pensive sad
😴 sleeping tired zzz
😷 mask sick
🤒 thermometer sick fever
🤕 bandage hurt injured
🤢 nauseated sick green
🤮 vomit sick
🤧 sneeze
🥵 hot overheated
🥶 cold freezing
🥴 woozy dizzy
😵 dizzy knocked out
🤯 mind blown exploding head
🤠 cowboy hat
🥳 party celebrate birthday
😎 cool sunglasses
🤓 nerd glasses
🧐 monocle curious
😕 confused
😟 worried
🙁 frown sad
☹ frowning sad
😮 surprised open mouth wow
😲 astonished shocked
😳 flushed embarrassed
🥺 pleading puppy eyes
😦 frowning open mouth
😨 fearful scared
😰 anxious sweat
😥 sad relieved
😢 cry sad tear
😭 sobbing crying loud
😱 scream shocked fear
😖 confounded
😞 disappointed
😓 downcast sweat
😩 weary tired
😫 tired exhausted
🥱 yawn bored sleepy
😤 triumph huffing frustrated
😡 angry red mad
😠 angry mad
🤬 cursing swearing angry
😈 devil smiling imp
💀 skull dead
☠ skull crossbones
💩 poop
🤡 clown
👻 ghost boo
👽 alien
🤖 robot
🙈 see no evil monkey
🙉 hear no evil monkey
🙊 speak no evil monkey
`),
      group('Hearts & symbols', '❤', `
❤ red heart love
🧡 orange heart
💛 yellow heart
💚 green heart
💙 blue heart
💜 purple heart
🖤 black heart
🤍 white heart
🤎 brown heart
💔 broken heart
❣ heart exclamation
💕 two hearts love
💞 revolving hearts
💓 beating heart
💗 growing heart
💖 sparkling heart
💘 heart arrow cupid
💝 heart ribbon gift
💯 hundred perfect score
💢 anger
💥 boom collision
💫 dizzy stars
💦 sweat droplets water
💨 dash wind
💬 speech bubble chat comment
💭 thought bubble
💤 zzz sleep
✅ check mark done yes complete
❌ cross x no wrong
❎ cross mark button
⭕ circle
❗ exclamation important
❓ question
‼ double exclamation
⁉ exclamation question
⚠ warning caution
🚫 prohibited no
⛔ no entry stop
✔ check heavy
➕ plus add
➖ minus
✖ multiply
♻ recycle
🔴 red circle
🟠 orange circle
🟡 yellow circle
🟢 green circle
🔵 blue circle
🟣 purple circle
⚫ black circle
⚪ white circle
🔺 red triangle up
🔻 red triangle down
▶ play
⏸ pause
⏹ stop
🔁 repeat loop
🔔 bell notification
🔕 bell off mute
🔊 loud speaker volume
📣 megaphone announce
🏁 finish checkered flag
🚩 flag red triangular
🆗 ok button
🆕 new
🆒 cool
🆘 sos help
ℹ information
🔝 top
`),
      group('People & hands', '👍', `
👍 thumbs up like yes approve
👎 thumbs down dislike no
👌 ok perfect fine
🤌 pinched fingers italian
🤏 pinching small
✌ victory peace
🤞 crossed fingers luck hope
🤟 love you gesture
🤘 rock on horns
🤙 call me shaka
👈 point left
👉 point right
👆 point up
👇 point down
☝ index up
✋ raised hand stop high five
🤚 raised back of hand
🖐 hand fingers splayed
🖖 vulcan salute spock
👋 wave hello goodbye
🤝 handshake deal agreement
👏 clap applause bravo
🙌 raising hands celebrate hooray
👐 open hands
🤲 palms up
🙏 pray thanks please folded hands
✍ writing hand
💪 muscle strong flex biceps
🧠 brain smart
👀 eyes look watching
👁 eye
👂 ear listen
👄 mouth lips
🙋 raising hand question
🙆 ok gesture
🙅 no gesture
🤷 shrug dont know
🤦 facepalm
🙇 bow sorry thank
💁 information desk
🧑‍💻 technologist developer computer
👷 construction worker builder
🕵 detective
🧑‍🚀 astronaut
`),
      group('Animals & nature', '🌳', `
🐶 dog
🐱 cat
🐭 mouse
🐹 hamster
🐰 rabbit bunny
🦊 fox
🐻 bear
🐼 panda
🐨 koala
🐯 tiger
🦁 lion
🐮 cow
🐷 pig
🐸 frog
🐵 monkey
🐔 chicken
🐧 penguin
🐦 bird
🦅 eagle
🦉 owl
🐝 bee honey
🐛 bug caterpillar
🦋 butterfly
🐌 snail slow
🐞 ladybug
🐢 turtle slow
🐍 snake
🐙 octopus
🐬 dolphin
🐳 whale
🦈 shark
🌵 cactus desert
🌲 evergreen tree
🌳 tree
🌴 palm tree
🍀 four leaf clover luck
🌸 cherry blossom
🌹 rose
🌻 sunflower
🔥 fire hot lit flame
⭐ star
🌟 glowing star
✨ sparkles shiny new
⚡ lightning bolt zap fast energy
🌈 rainbow
☀ sun sunny
⛅ sun behind cloud
☁ cloud
🌧 rain cloud
⛈ thunderstorm
❄ snowflake cold winter
🌊 wave water ocean
💧 droplet water
🌙 moon night
🌍 earth globe world
`),
      group('Food & drink', '🍕', `
🍎 apple red
🍊 orange tangerine
🍋 lemon
🍌 banana
🍉 watermelon
🍇 grapes
🍓 strawberry
🍒 cherries
🍑 peach
🍍 pineapple
🥑 avocado
🍅 tomato
🥕 carrot
🌽 corn
🥔 potato
🍞 bread
🧀 cheese
🍳 egg cooking breakfast
🥞 pancakes
🥓 bacon
🍔 burger hamburger
🍟 fries
🍕 pizza
🌭 hot dog
🥪 sandwich
🌮 taco
🌯 burrito
🍣 sushi
🍜 noodles ramen
🍝 spaghetti pasta
🍰 cake slice
🎂 birthday cake
🍪 cookie
🍩 doughnut
🍫 chocolate
🍿 popcorn
☕ coffee hot drink
🍵 tea
🍺 beer
🍻 cheers beers
🥂 champagne toast clink
🍷 wine
🥃 whiskey tumbler
🍸 cocktail martini
🍹 tropical drink
🥤 cup with straw soda
`),
      group('Activities & places', '🚀', `
⚽ soccer football
🏀 basketball
🏈 american football
⚾ baseball
🎾 tennis
🏐 volleyball
⛳ golf
🏆 trophy win winner champion
🥇 gold medal first
🥈 silver medal second
🥉 bronze medal third
🎯 bullseye target direct hit
🎮 video game controller
🎲 dice game
🎨 art palette
🎬 clapper movie film
🎤 microphone
🎧 headphones music
🎸 guitar
🎹 piano keyboard
🥁 drum
🎉 party popper celebrate congratulations tada
🎊 confetti ball
🎁 gift present
🎈 balloon
🚗 car
🚕 taxi
🚌 bus
🚑 ambulance
🚒 fire truck
🚚 delivery truck
🚜 tractor
🚲 bicycle
✈ airplane flight travel
🚀 rocket launch
🚁 helicopter
⛵ sailboat
🚢 ship
🚂 train
🏠 house home
🏢 office building
🏗 construction building crane
🏭 factory
🏥 hospital
🏦 bank
🏨 hotel
🗽 statue of liberty
🌆 city dusk
🌉 bridge night
⛽ fuel pump gas
🚧 construction barrier
🚦 traffic light
🏖 beach
⛰ mountain
`),
      group('Objects', '💡', `
⌚ watch
📱 mobile phone
💻 laptop computer
⌨ keyboard
🖥 desktop computer
🖨 printer
🖱 mouse computer
💾 floppy save disk
📷 camera
🎥 movie camera
📞 telephone receiver call
☎ telephone
📺 television
⏰ alarm clock
⏳ hourglass time
💡 light bulb idea
🔋 battery
🔌 plug
💰 money bag
💵 dollar cash
💳 credit card
💎 gem diamond
⚖ scales balance legal
🔧 wrench tool
🔨 hammer
⚙ gear settings
🧰 toolbox
🔒 locked secure
🔓 unlocked
🔑 key
📌 pushpin pin
📍 round pushpin location
📎 paperclip attachment
✂ scissors cut
📏 ruler
📐 triangle ruler
📝 memo note write
✏ pencil
🖊 pen
📁 folder
📂 open folder
📅 calendar date
📆 tear off calendar
📊 bar chart stats
📈 chart increasing growth up
📉 chart decreasing down
📋 clipboard
📄 page document
📰 newspaper news
📚 books
📖 open book
🔖 bookmark
🏷 label tag
✉ envelope email mail
📧 email
📦 package box delivery
📮 postbox
🗑 wastebasket trash delete
🧹 broom clean
🧪 test tube
🔬 microscope
🔭 telescope
`),
    ],
  };
})();
