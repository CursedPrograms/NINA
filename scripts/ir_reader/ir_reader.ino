// ir_reader.ino - what NINA's IR reader flashes onto a board: prints the code of each remote button.
// She chooses the pin when she builds it, because each robot's receiver is on its own.
#include <Arduino.h>
#include <IRremote.hpp>

#ifndef IR_PIN
#define IR_PIN 6
#endif

void setup() {
    Serial.begin(115200);
    IrReceiver.begin(IR_PIN, ENABLE_LED_FEEDBACK);   // the L LED flashes when it hears IR
    Serial.print(F("IR reader ready on pin "));
    Serial.println(IR_PIN);
}

// The fleet's handshake (board_id.py): the PC says WHO, the board says who it is
void answerWho() {
    static String line;
    while (Serial.available()) {
        const char c = Serial.read();
        if (c != '\n') {
            if (c != '\r' && line.length() < 12) line += c;
            continue;
        }
        if (line == "WHO" || line == "WHOAMI") Serial.println(F("I am Nina"));
        line = "";
    }
}

void loop() {
    answerWho();
    if (IrReceiver.decode()) {
        IrReceiver.printIRResultShort(&Serial);
        if (IrReceiver.decodedIRData.command != 0) {
            Serial.print(F(" -> Command HEX: 0x"));
            Serial.println(IrReceiver.decodedIRData.command, HEX);
        } else if (IrReceiver.decodedIRData.decodedRawData != 0) {
            Serial.print(F(" -> Raw Data HEX: 0x"));
            Serial.println(IrReceiver.decodedIRData.decodedRawData, HEX);
        }
        IrReceiver.resume();
    }
}
