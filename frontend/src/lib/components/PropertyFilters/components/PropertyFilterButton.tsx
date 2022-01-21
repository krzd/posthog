import { Button } from 'antd'
import { useValues } from 'kea'
import { formatPropertyLabel } from 'lib/utils'
import React from 'react'
import { cohortsModel } from '~/models/cohortsModel'
import { AnyPropertyFilter } from '~/types'
import { keyMapping } from 'lib/components/PropertyKeyInfo'
import clsx from 'clsx'
import { propertyDefinitionsModel } from '~/models/propertyDefinitionsModel'
import { PushpinOutlined } from '@ant-design/icons'

export interface PropertyFilterButtonProps {
    item: AnyPropertyFilter
    greyBadges?: boolean
    onClick?: () => void
    setRef?: (ref: HTMLElement) => void
    pinned?: boolean
}

export function PropertyFilterButton({ item, ...props }: PropertyFilterButtonProps): JSX.Element {
    const { cohorts } = useValues(cohortsModel)
    const { formatForDisplay } = useValues(propertyDefinitionsModel)

    return (
        <FilterButton {...props}>
            {formatPropertyLabel(item, cohorts, keyMapping, (s) => formatForDisplay(item.key, s))}
        </FilterButton>
    )
}

interface FilterRowProps {
    greyBadges?: boolean
    onClick?: () => void
    setRef?: (ref: HTMLElement) => void
    children: string | JSX.Element
    pinned?: boolean
}

export function FilterButton({ greyBadges, onClick, setRef, children, pinned }: FilterRowProps): JSX.Element {
    return (
        <Button
            type="primary"
            shape="round"
            style={{ overflow: 'hidden' }}
            onClick={pinned ? () => {} : onClick}
            ref={setRef}
            className={clsx('property-filter', (greyBadges || pinned) && 'property-filter-grey pinned-filter')}
        >
            <span
                className="ph-no-capture property-filter-button-label"
                style={{
                    width: '100%',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                }}
            >
                {children}
                {pinned && <PushpinOutlined style={{ marginLeft: '.5em' }} />}
            </span>
        </Button>
    )
}

export default PropertyFilterButton
