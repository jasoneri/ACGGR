#!/bin/bash
if [ ! -x "acggr" ]; then
    curl -fsSL https://raw.githubusercontent.com/jasoneri/acggr/pyapp/deploy/launcher/mac/init.bash | bash && acggr
else
    acggr
fi
