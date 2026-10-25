# Check resolved Vivado properties AND actual BD nets after validate_bd_design.
# Do not set derived/read-only reset-polarity properties. PG164 defines auxiliary
# reset polarity 0 as active-low and debug reset as unconditionally active-high.
proc siliconsign_assert_reset_wiring {reset one zero} {
    foreach property {CONFIG.C_EXT_RESET_HIGH CONFIG.C_AUX_RESET_HIGH} {
        if {[get_property $property $reset] ne "0"} {
            error "Reset input polarity is not the required ACTIVE_LOW ($property); refusing synthesis."
        }
    }
    foreach {cell expected} [list $one 1 $zero 0] {
        if {[get_property CONFIG.CONST_WIDTH $cell] ne "1" ||
            [get_property CONFIG.CONST_VAL $cell] ne "$expected"} {
            error "Reset tie-off is not the expected one-bit constant; refusing synthesis."
        }
    }
    foreach {input source} [list \
        "$reset/aux_reset_in" "$one/dout" \
        "$reset/dcm_locked" "$one/dout" \
        "$reset/mb_debug_sys_rst" "$zero/dout"] {
        set input_pin [get_bd_pins -quiet $input]
        set source_pin [get_bd_pins -quiet $source]
        if {[llength $input_pin] != 1 || [llength $source_pin] != 1} {
            error "Reset input or tie-off pin is missing; refusing synthesis."
        }
        set input_net [get_bd_nets -quiet -of_objects $input_pin]
        set source_net [get_bd_nets -quiet -of_objects $source_pin]
        if {[llength $input_net] != 1 || [llength $source_net] != 1 ||
            $input_net ne $source_net} {
            error "Reset input $input is not connected to its inactive/locked constant; refusing synthesis."
        }
    }
    puts "Verified active-low external/auxiliary reset, inactive auxiliary/debug tie-offs and asserted clock-lock input."
}
